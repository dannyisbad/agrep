//! Gemini CLI adapter: ~/.gemini/tmp/<projectHash>/chats/session-*.jsonl (and legacy .json)
//!
//! Shapes follow google-gemini/gemini-cli packages/core/src/services/chatRecordingService.ts
//! (fb972b2). Since #23749 a session is JSONL: a metadata line `{ sessionId, projectHash,
//! startTime, ... }`, then whole message records - re-recording a message (tool calls, tokens)
//! appends it again under the same `id`; the last write wins at its first position - plus
//! `$set` (metadata; until 2026-10 also whole-history `messages` checkpoints), `$rewindTo` (drop
//! that message and every later one; an unknown id drops them all) and `$patch` (content and
//! tool-result updates, `removeIds`, `orderIds`). Checkpoints and `removeIds` re-sync the file to
//! the model's context: an aborted or failed request removes its own turns, while compression
//! (`ChatCompressionService`, automatic past a token threshold or `/compress`), tool-output
//! masking, truncation and `/chat resume` re-record the context under new ids and remove every
//! earlier id. The adapter keeps the transcript (see `Fold`); a compression's `<state_snapshot>`
//! user turn becomes a recap row. Resuming a legacy session copies `X.json`
//! into `X.jsonl` beside it and leaves the `.json` behind, so a `.json` with a `.jsonl`
//! sibling is superseded and not a source. The legacy store is one JSON object per session:
//!   { sessionId, projectHash, startTime, lastUpdated, messages: [ ... ] }
//! each message is `{ id, timestamp (RFC3339), type, content, displayContent?, ... }` and
//! `content` is a string or, since early 2026, a Part[] such as `[{"text": ...}]`:
//!   type=="user"   -> the human's prompt; `displayContent` holds what was typed when the sent
//!                     request differs (expanded @file context). Tool results are recorded as
//!                     user turns of functionResponse parts, the environment preamble as a
//!                     `<session_context>` user turn and IDE mode's editor context as one of its
//!                     own - none is the human
//!   type=="gemini" -> the model's turn: `content` prose reply (thought parts excluded),
//!                     `model`, `toolCalls[]`, plus `thoughts` (reasoning, excluded) and `tokens`
//!   type=="info"   -> CLI system notices (auth flow, etc.) - not the user, skipped
//! a toolCall is `{ id, name, args, result: [{functionResponse:{response:{output}}}],
//! status }`. projectHash is a SHA256 of the project dir with no reverse map in the store,
//! so project attribution falls back to "gemini" (see collect).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::ingest::{cap_event_output, is_wrapper, summarize_tool_input_with_chars};
use crate::ingest_cache::ReadOutcome;
use crate::intake::{Skip, Tally};
use crate::model::{Event, Message};

/// `result[*].functionResponse.response.output` of one result part.
fn function_output(part: &Value) -> Option<&str> {
    part.get("functionResponse")?
        .get("response")?
        .get("output")?
        .as_str()
}

/// A toolCall's captured output + outcome: `result[*].functionResponse.response.output`,
/// with `status` giving the ok signal when the store recorded one. `result` is a
/// PartListUnion, so a bare string or a single part is accepted too.
fn tool_result(tc: &Value) -> (String, usize, usize, Option<bool>) {
    let outputs: Vec<&str> = match tc.get("result") {
        Some(Value::String(text)) => vec![text.as_str()],
        Some(Value::Array(parts)) => parts.iter().filter_map(function_output).collect(),
        Some(part @ Value::Object(_)) => function_output(part).into_iter().collect(),
        _ => Vec::new(),
    };
    let ok = match tc.get("status").and_then(|s| s.as_str()) {
        Some("success") => Some(true),
        Some("error") | Some("failed") => Some(false),
        _ => None,
    };
    let (output, output_chars, output_bytes) = cap_event_output(&outputs.join("\n"));
    (output, output_chars, output_bytes, ok)
}

/// The text a PartListUnion carries: the string itself, or its text parts joined the way
/// upstream's `partToString` joins them. Thought parts are reasoning, never prose.
fn part_text(value: &Value) -> String {
    parts_text(parts_of(value))
}

fn parts_text(parts: &[Value]) -> String {
    let mut out = String::new();
    for text in parts.iter().filter_map(prose) {
        out.push_str(text);
    }
    out
}

/// A part's text when it is prose: a string, or a text part that is no thought.
fn prose(part: &Value) -> Option<&str> {
    match part {
        Value::String(text) => Some(text),
        Value::Object(fields) => {
            let thought = fields
                .get("thought")
                .is_some_and(|flag| !matches!(flag, Value::Null | Value::Bool(false)));
            fields
                .get("text")
                .and_then(Value::as_str)
                .filter(|_| !thought)
        }
        _ => None,
    }
}

/// A model turn's recorded reply, moved out of its content: the string upstream records, or the
/// prose of the parts a re-sync left (see `with_recorded_reply`).
fn recorded_text(content: Value) -> String {
    let parts = match content {
        Value::String(text) => return text,
        Value::Array(parts) => parts,
        part => vec![part],
    };
    if parts.iter().filter(|part| prose(part).is_some()).count() != 1 {
        return parts_text(&parts);
    }
    match parts.into_iter().find(|part| prose(part).is_some()) {
        Some(Value::String(text)) => text,
        Some(Value::Object(mut fields)) => match fields.remove("text") {
            Some(Value::String(text)) => text,
            _ => String::new(),
        },
        _ => String::new(),
    }
}

/// A model turn's parts as a re-sync writes them back, with the reply upstream recorded in place
/// of their prose: the first prose part carries it and the others go, so the turn keeps both its
/// cleaned reply and the calls a copy of it repeats.
fn with_recorded_reply(parts: &Value, reply: String) -> Value {
    let mut reply = Some(reply);
    let mut out = Vec::new();
    for part in parts_of(parts) {
        if prose(part).is_none() {
            out.push(part.clone());
        } else if let Some(text) = reply.take() {
            let mut part = part.clone();
            match &mut part {
                Value::Object(fields) => {
                    fields.insert("text".to_string(), Value::String(text));
                }
                bare => *bare = Value::String(text),
            }
            out.push(part);
        }
    }
    Value::Array(out)
}

/// A PartListUnion as its parts: a lone string or part is a list of one.
fn parts_of(value: &Value) -> &[Value] {
    match value {
        Value::Array(parts) => parts,
        part => std::slice::from_ref(part),
    }
}

/// A user turn carrying a functionResponse part is a tool result, not something typed.
fn carries_function_response(value: &Value) -> bool {
    match value {
        Value::Array(parts) => parts
            .iter()
            .any(|part| part.get("functionResponse").is_some()),
        part => part.get("functionResponse").is_some(),
    }
}

/// Machine-written user turns: upstream `isIgnoredUserContent`'s prefixes (the environment
/// preamble every new session records first, hook-injected context) and, in IDE mode, the editor
/// context `GeminiClient.getIdeContextParts` sends as its own turn before a prompt (client.ts
/// 484-485 full, 594-595 changes, joined at 751 at fb972b2).
fn is_injected_context(text: &str) -> bool {
    const PREFIXES: [&str; 4] = [
        "<session_context>",
        "<hook_context>",
        "Here is the user's editor context as a JSON object. This is for your information only.",
        "Here is a summary of changes in the user's editor context, in JSON format. This is for \
         your information only.",
    ];
    let text = text.trim_start();
    PREFIXES.iter().any(|prefix| text.starts_with(prefix))
}

/// A user turn's parts past its leading injected context when more follows: `getHistory()`
/// coalesces the context turn into the prompt after it for Gemini 2 and 3 models.
fn past_injected(mut parts: &[Value]) -> &[Value] {
    while let Some((first, rest)) = parts.split_first() {
        if rest.is_empty() || !is_injected_context(&parts_text(std::slice::from_ref(first))) {
            break;
        }
        parts = rest;
    }
    parts
}

/// An info, warning or error item of the CLI's own: `useHistory().addItem` records every one as a
/// session message with its text as content (useHistoryManager.ts 91-124 at fb972b2), outside the
/// model's history, so the next re-sync removes it. Auto-compression's can stay for good: the UI
/// records it through the recorder it bound before `tryCompressChat` swapped in a new one
/// (AppContainer.tsx 235-237, client.ts 1236-1250), and the new one never learns its id.
fn is_notice(message: &Value) -> bool {
    is_info_type(message) && !is_binary_turn(message)
}

fn is_info_type(message: &Value) -> bool {
    matches!(
        message.get("type").and_then(Value::as_str),
        Some("info" | "error" | "warning")
    )
}

/// The binary data a read_file of audio or video injects: `sendMessageStream` records it as an info
/// message of parts (geminiChat.ts 613-621) but keeps it as a user turn of the model's history, so
/// its copies are user messages.
fn is_binary_turn(message: &Value) -> bool {
    message.get("type").and_then(Value::as_str) == Some("info")
        && message.get("content").is_some_and(Value::is_array)
}

/// The role a message has in the model's history: a binary turn is a user one.
fn history_kind(message: &Value) -> &str {
    if is_binary_turn(message) {
        return "user";
    }
    message.get("type").and_then(Value::as_str).unwrap_or("")
}

/// A message upstream's `convertSessionToClientHistory` (sessionUtils.ts 110-228 at fb972b2) leaves
/// out of the history it rebuilds on `/rewind` and `--resume`: info, error and warning messages,
/// binary turns too, and user turns `isIgnoredUserContent` (97-105) rejects. That tests the
/// trimmed `partListUnionToString`: `partToString` verbose (partUtils.ts 18-82), parts joined.
fn conversion_skips(message: &Value) -> bool {
    if is_info_type(message) {
        return true;
    }
    if message.get("type").and_then(Value::as_str) != Some("user") {
        return false;
    }
    let mut text = String::new();
    for part in parts_of(message.get("content").unwrap_or(&Value::Null)) {
        // verbose describes every non-text part as `[…]`: only that leading `[` can matter here
        const DESCRIBED: [&str; 8] = [
            "videoMetadata",
            "thought",
            "codeExecutionResult",
            "executableCode",
            "fileData",
            "functionCall",
            "functionResponse",
            "inlineData",
        ];
        match part {
            Value::String(piece) => text.push_str(piece),
            Value::Object(fields) if DESCRIBED.iter().any(|key| fields.contains_key(*key)) => {
                text.push('[');
            }
            Value::Object(fields) => match fields.get("text") {
                Some(Value::String(piece)) => text.push_str(piece),
                None | Some(Value::Null) => {}
                Some(_) => text.push('#'),
            },
            _ => {}
        }
    }
    // JS `trim`: Unicode White_Space without U+0085, plus U+FEFF
    let text =
        text.trim_start_matches(|c: char| (c.is_whitespace() && c != '\u{85}') || c == '\u{FEFF}');
    text.is_empty()
        || ["/", "?", "<session_context>", "<hook_context>"]
            .iter()
            .any(|prefix| text.starts_with(prefix))
}

/// Compression (upstream `ChatCompressionService`) records the model-written `<state_snapshot>`
/// as a user turn, then this canned model acknowledgement, before the kept tail.
const COMPRESSION_ACK: &str = "Got it. Thanks for the additional context!";

fn is_state_snapshot(text: &str) -> bool {
    text.contains("<state_snapshot>") && text.contains("</state_snapshot>")
}

fn file_stem(path: &Path) -> String {
    path.file_stem()
        .map(|stem| stem.to_string_lossy().to_string())
        .unwrap_or_default()
}

/// One session's messages as rows and events; a message flagged `true` only repeats an earlier
/// one. Callers have already counted every message as seen; each one here lands in exactly one
/// row, agent row or named skip.
fn emit_messages<'a>(
    session: &str,
    messages: impl IntoIterator<Item = (&'a Value, bool)>,
    tally: &Tally,
) -> (Vec<Message>, Vec<Event>) {
    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut recap_turns: Vec<u32> = Vec::new();
    let mut turn = 0u32;
    for (message_ordinal, (m, replay)) in messages.into_iter().enumerate() {
        if replay {
            tally.skip(Skip::Replay);
            continue;
        }
        let ty = m.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let ts = parse_timestamp::rfc3339(m.get("timestamp").and_then(|t| t.as_str()));
        match ty {
            "user" => {
                let content = m.get("content").unwrap_or(&Value::Null);
                if carries_function_response(content) {
                    tally.skip(Skip::NonHuman);
                    continue;
                }
                let typed = m
                    .get("displayContent")
                    .map(part_text)
                    .filter(|text| !text.trim().is_empty());
                let text = typed.unwrap_or_else(|| parts_text(past_injected(parts_of(content))));
                if text.trim().is_empty() {
                    tally.skip(Skip::EmptyText);
                    continue;
                }
                if is_wrapper(&text) || is_injected_context(&text) {
                    tally.skip(Skip::Wrapper);
                    continue;
                }
                tally.row();
                if is_state_snapshot(&text) {
                    recap_turns.push(turn);
                }
                out.push(crate::model::RawMessage {
                    agent: "gemini",
                    project: "gemini".to_string(),
                    session: session.to_string(),
                    ts,
                    turn,
                    text,
                    model: String::new(),
                    reply: String::new(),
                    reply_chars: 0,
                    side: false,
                    parent: String::new(),
                });
                turn += 1;
            }
            "gemini" => {
                // prose reply -> the user turn it answers (thoughts are reasoning, excluded)
                let txt = m.get("content").map(part_text).unwrap_or_default();
                // canned CLI text: a copy may land away from the snapshot it acknowledged
                let canned = m.get("toolCalls").is_none()
                    && matches!(txt.trim(), COMPRESSION_ACK | INTERRUPTED_PLACEHOLDER);
                if canned {
                    tally.skip(Skip::Meta);
                    continue;
                }
                if !txt.trim().is_empty() {
                    if let Some(last) = out.last_mut() {
                        let chars = crate::ingest::append_capped(
                            &mut last.reply,
                            &txt,
                            crate::ingest::REPLY_CAP,
                        );
                        last.reply_chars += chars;
                    }
                }
                if let Some(last) = out.last_mut() {
                    let recap = recap_turns.last() == Some(&last.turn);
                    if last.model.is_empty() && !recap {
                        if let Some(md) = m.get("model").and_then(|v| v.as_str()) {
                            if !md.is_empty() {
                                last.model = md.to_string();
                            }
                        }
                    }
                }
                if let Some(calls) = m.get("toolCalls").and_then(|c| c.as_array()) {
                    for (call_ordinal, tc) in calls.iter().enumerate() {
                        let name = tc.get("name").and_then(|n| n.as_str()).unwrap_or("?");
                        let (input, input_chars) = tc
                            .get("args")
                            .map(summarize_tool_input_with_chars)
                            .unwrap_or_default();
                        let (output, output_chars, output_bytes, ok) = tool_result(tc);
                        tally.event();
                        events.push(Event {
                            agent: "gemini",
                            session: session.to_string(),
                            ts,
                            kind: "tool",
                            name: name.to_string(),
                            input,
                            output,
                            input_chars,
                            output_chars,
                            output_bytes,
                            ok,
                            call_id: tc
                                .get("id")
                                .and_then(|i| i.as_str())
                                .filter(|id| !id.trim().is_empty())
                                .map(str::to_string)
                                .unwrap_or_else(|| {
                                    format!("gemini:{message_ordinal}:{call_ordinal}")
                                }),
                            child_session: String::new(),
                            meta: String::new(),
                        });
                    }
                }
                tally.agent_row();
            }
            "info" => tally.skip(Skip::Meta),
            _ => tally.skip(Skip::NonHuman),
        }
    }
    let messages = out
        .into_iter()
        .map(|raw| {
            let recap = recap_turns.binary_search(&raw.turn).is_ok();
            let mut message = raw.freeze();
            if recap {
                message.who = "recap".into();
                message.model_source = "recap".into();
            }
            message
        })
        .collect();
    (messages, events)
}

/// A legacy whole-session document; seen = elements of its messages array.
fn parse_document(root: &Value, path: &Path, tally: &Tally) -> (Vec<Message>, Vec<Event>) {
    let session = root
        .get("sessionId")
        .and_then(|s| s.as_str())
        .map(str::to_string)
        .unwrap_or_else(|| file_stem(path));
    let Some(messages) = root.get("messages").and_then(|m| m.as_array()) else {
        return (Vec::new(), Vec::new());
    };
    tally.seen_n(messages.len() as u64);
    emit_messages(&session, messages.iter().map(|m| (m, false)), tally)
}

/// JS `typeof value === 'object' && value !== null`, the test upstream applies to `$set`/`$patch`.
fn is_js_object(value: &Value) -> bool {
    matches!(value, Value::Object(_) | Value::Array(_))
}

fn js_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|n| n != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(_) | Value::Object(_) => true,
    }
}

/// Text as upstream records a reply (`responseText`): zero-width characters removed, then HTML
/// comments until none is left, then trimmed. One pass: a comment closes at the first `-->` after
/// the leftmost open `<!--`, and a removal can join the text around it into a new `<!--`.
fn visible_text(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut open: Option<usize> = None;
    for c in text.chars() {
        if matches!(
            c,
            '\u{200B}'..='\u{200D}' | '\u{FEFF}' | '\u{200E}' | '\u{200F}'
        ) {
            continue;
        }
        out.push(c);
        match open {
            None if out.ends_with("<!--") => open = Some(out.len() - 4),
            Some(start) if out.len() >= start + 7 && out.ends_with("-->") => {
                out.truncate(start);
                open = None;
            }
            _ => {}
        }
    }
    out.trim().to_string()
}

/// Upstream's interrupted-turn closer (`GeminiChat.closeUnansweredToolResponseTurn`): a model turn
/// the CLI appends after a cancelled tool call, never something the model said.
const INTERRUPTED_PLACEHOLDER: &str =
    "[The previous response was interrupted before it completed.]";

/// Where one recorded message stands.
#[derive(Clone, Copy, PartialEq, Eq)]
enum State {
    /// In upstream's context map.
    Live,
    /// Out of the context after a rewrite, still part of the conversation.
    Dropped,
    /// Undone: rewound, or rolled back after an aborted or failed request.
    Deleted,
}

struct Entry {
    message: Value,
    state: State,
    /// The entries this one re-records under a new id during a context rewrite: one, or the run
    /// of consecutive turns of one role upstream's `getHistory()` coalesced into it.
    originals: Vec<usize>,
    /// Position in `Fold::order` while in the context map.
    slot: Option<usize>,
    /// The `Fold::epoch` in which the entry entered the map.
    entered: u64,
    /// A model turn's visible reply, once `update` has read it: patches that write the same back
    /// can come by the hundred thousand.
    reply: Option<String>,
    /// Opens an exchange (see `opens_exchange`): fixed when recorded and cleared when paired as a
    /// copy, so the boundaries `undo_answers` scans between never appear later.
    row: bool,
}

/// A prompt or a snapshot, as `emit_messages` reads a row's message: the model turns after it, up
/// to the next one, answer it. A copy, which emits no row, is no such message once paired.
fn opens_exchange(message: &Value) -> bool {
    let content = message.get("content").unwrap_or(&Value::Null);
    if message.get("type").and_then(Value::as_str) != Some("user")
        || carries_function_response(content)
    {
        return false;
    }
    let typed = message
        .get("displayContent")
        .map(part_text)
        .filter(|text| !text.trim().is_empty());
    let text = typed.unwrap_or_else(|| parts_text(past_injected(parts_of(content))));
    !text.trim().is_empty() && !is_wrapper(&text) && !is_injected_context(&text)
}

/// What a re-recorded copy shares with the message it repeats: type, visible text and the
/// (name, call id) of every tool call or result it carries.
type CopyKey = (String, String, Vec<(String, String)>);

/// One part of a turn as a coalesced copy repeats it: whether the turn is the model's, the part's
/// text and its tool call or result.
type PartKey = (bool, String, Option<(String, String)>);

/// A message's parts as a coalesced copy repeats them, in history order. A model turn's record
/// keeps its cleaned reply as text and its calls in `toolCalls` while a copy keeps the turn's parts,
/// so both read as visible text (see `visible_text`) and calls; coalescing strips thoughts first.
fn part_keys(message: &Value) -> Vec<PartKey> {
    let Some(content) = message.get("content") else {
        return Vec::new();
    };
    let text = |part: &Value| parts_text(std::slice::from_ref(part));
    match history_kind(message) {
        "user" => parts_of(content)
            .iter()
            .map(|part| (false, text(part), part_tool(part)))
            .collect(),
        "gemini" => {
            let mut keys = Vec::new();
            for part in parts_of(content) {
                if let Some(call) = part.get("functionCall") {
                    keys.push((true, String::new(), Some(name_and_id(call))));
                    continue;
                }
                let reply = visible_text(&text(part));
                if !reply.is_empty() {
                    keys.push((true, reply, None));
                }
            }
            if !keys.iter().any(|(_, _, call)| call.is_some()) {
                let calls = message.get("toolCalls").and_then(Value::as_array);
                for call in calls.into_iter().flatten() {
                    keys.push((true, String::new(), Some(name_and_id(call))));
                }
            }
            keys
        }
        _ => Vec::new(),
    }
}

/// The (name, call id) of a functionCall or functionResponse part.
fn part_tool(part: &Value) -> Option<(String, String)> {
    ["functionCall", "functionResponse"]
        .iter()
        .find_map(|kind| part.get(*kind))
        .map(name_and_id)
}

fn name_and_id(call: &Value) -> (String, String) {
    let field = |name: &str| {
        call.get(name)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    (field("name"), field("id"))
}

fn key_hash(key: &CopyKey) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    key.hash(&mut hasher);
    hasher.finish()
}

/// Upstream's `createJsonlRecordAccumulator` (full-load mode) mirrored as `order`, its context map
/// in insertion order with record kinds tested in its order, beside the transcript (`entries`, in
/// first-recorded order) that a person read. A re-sync (`$patch` `removeIds`/`orderIds`, or a
/// `$set.messages` checkpoint) that removes messages splits them:
/// - a tail rollback (aborted and failed requests): the removed messages that were the last ones
///   in the map before it, when nothing it records lands after the surviving ones once its order
///   applies. They are undone, with the originals of copies among them. `$rewindTo` is one too,
///   and only ever touches the map: dropped messages after its target stay.
/// - a context rewrite (compression, tool-output masking, truncation, `/chat resume`), the rest:
///   the removed messages stay, and the copies recorded since the previous re-sync after them pair
///   to them, or to messages dropped earlier, as replays. A re-sync never overwrites a recorded
///   tool result.
///
/// Two removals are neither, and their messages stay, as dropped: the re-sync right after a
/// `/rewind` or `--resume`, which removes only what upstream's conversion of the history skips
/// (see `converting`), and a declined tool's rollback (see `declines`).
///
/// Work stays linear in the file: each entry is keyed at most once as removed and once as a
/// copy, and the map is a slot list compacted as removals accumulate and wherever the rollback
/// walk crossed a gap, so no gap is walked twice.
/// Every non-blank line and every message inside a `messages` array is one seen unit, and each
/// leaves through exactly one counter below or as one transcript entry.
#[derive(Default)]
struct Fold {
    session_id: Option<Value>,
    project_hash: Option<Value>,
    entries: Vec<Entry>,
    /// Upstream's context map: entry indices in its insertion order, `None` where one left.
    order: Vec<Option<usize>>,
    in_map: usize,
    /// Map entries that are not notices (see `is_notice`).
    turns_in_map: usize,
    /// Each id's newest entry.
    by_id: HashMap<String, usize>,
    /// Bumped by every record that is not a message: entries entering the map in the current
    /// epoch were recorded by the re-sync that follows them.
    epoch: u64,
    /// `entries.len()` at the previous re-sync; copies are recorded after it.
    mark: usize,
    /// Map entries other than notices recorded before `mark`: none left after a re-sync means it
    /// re-recorded the whole context (`setHistory` with fresh ids: masking, truncation,
    /// `/chat resume`). A notice can outlive any re-sync (see `is_notice`).
    before_mark: usize,
    /// The epoch of the previous re-sync or `$rewindTo` that removed a message other than a notice
    /// (see `declines`). A re-sync that only adds, as the `$set.messages` recorder writes on
    /// compressing an empty context, leaves it, and the environment's stable id can re-enter after.
    removal_epoch: Option<u64>,
    /// Set by a `$rewindTo` or a `$set` of `sessionId` until the next record that is not a
    /// message. `/rewind` writes the first, then re-syncs to `convertSessionToClientHistory` of
    /// what is left (rewindCommand.tsx 47-60 at fb972b2); a recorder taking over the file for
    /// `--resume` or compression writes the second (chatRecordingService.ts 737), and `--resume`
    /// then re-syncs to that conversion of the file (useSessionResume.ts 92, 122). A re-sync then
    /// that removes only what the conversion skips is that one.
    converting: bool,
    /// Dropped entries by `CopyKey` hash, the pool a whole-context rewrite pairs from: a
    /// `/chat resume` of a save from before a compression copies messages already out of the
    /// context. Entries no longer dropped are pruned on use.
    dropped: HashMap<u64, BTreeSet<usize>>,
    seen: u64,
    meta: u64,
    non_message: u64,
    replay: u64,
    errors: u64,
    first_error: Option<String>,
}

#[deny(clippy::indexing_slicing)]
impl Fold {
    fn read(data: &str) -> Self {
        let mut fold = Self::default();
        for line in data.lines() {
            if line.trim().is_empty() {
                continue;
            }
            fold.seen += 1;
            match serde_json::from_str::<Value>(line) {
                Ok(record) => fold.apply(record),
                Err(error) => {
                    fold.errors += 1;
                    if fold.first_error.is_none() {
                        fold.first_error =
                            Some(format!("{error}: {}", crate::intake::clip(line, 80)));
                    }
                }
            }
        }
        fold
    }

    fn apply(&mut self, record: Value) {
        let converting = std::mem::take(&mut self.converting);
        if let Some(target) = record.get("$rewindTo").and_then(Value::as_str) {
            self.meta += 1;
            self.converting = self.rewind(target, converting);
        } else if let Some(patch) = record.get("$patch").filter(|patch| is_js_object(patch)) {
            self.meta += 1;
            self.patch(patch, converting);
        } else if let (Some(Value::String(id)), None) = (record.get("id"), record.get("$patch")) {
            let id = id.clone();
            self.put(id, record);
            // the conversion's re-sync first records the tool results it re-derives
            self.converting = converting;
            return;
        } else if let Some(set) = record.get("$set").filter(|set| is_js_object(set)) {
            self.meta += 1;
            if let Some(Value::Array(messages)) = set.get("messages") {
                self.checkpoint(messages, converting);
            }
            self.merge_metadata(set);
            self.converting = set.get("sessionId").is_some();
        } else if record.get("sessionId").is_some_and(Value::is_string)
            && record.get("projectHash").is_some_and(Value::is_string)
        {
            self.meta += 1;
            self.merge_metadata(&record);
            if let Some(Value::Array(messages)) = record.get("messages") {
                for message in messages {
                    self.seen += 1;
                    match (message.get("id"), message.get("$patch")) {
                        (Some(Value::String(id)), None) => self.put(id.clone(), message.clone()),
                        _ => self.non_message += 1,
                    }
                }
            }
        } else {
            self.non_message += 1;
        }
        self.epoch += 1;
    }

    fn merge_metadata(&mut self, fields: &Value) {
        if let Some(session_id) = fields.get("sessionId") {
            self.session_id = Some(session_id.clone());
        }
        if let Some(project_hash) = fields.get("projectHash") {
            self.project_hash = Some(project_hash.clone());
        }
    }

    /// A message record: a rewrite of a message in the map replaces it in place; anything else
    /// enters the map at its end, a dropped message returning to it.
    fn put(&mut self, id: String, message: Value) {
        if let Some(index) = self.by_id.get(&id).copied() {
            let epoch = self.epoch;
            if let Some(entry) = self
                .entries
                .get_mut(index)
                .filter(|entry| entry.state != State::Deleted)
            {
                let was_turn = !is_notice(&entry.message);
                entry.message = message;
                entry.reply = None;
                let turn = !is_notice(&entry.message);
                let revived = entry.state == State::Dropped;
                if revived {
                    entry.state = State::Live;
                    entry.entered = epoch;
                    self.enter(index);
                } else if was_turn != turn {
                    let before = usize::from(index < self.mark);
                    if turn {
                        self.turns_in_map += 1;
                        self.before_mark += before;
                    } else {
                        self.turns_in_map = self.turns_in_map.saturating_sub(1);
                        self.before_mark = self.before_mark.saturating_sub(before);
                    }
                }
                self.replay += 1;
                return;
            }
        }
        let index = self.entries.len();
        self.by_id.insert(id, index);
        let row = opens_exchange(&message);
        self.entries.push(Entry {
            message,
            state: State::Live,
            originals: Vec::new(),
            slot: None,
            entered: self.epoch,
            reply: None,
            row,
        });
        self.enter(index);
    }

    fn enter(&mut self, index: usize) {
        if let Some(entry) = self.entries.get_mut(index) {
            entry.slot = Some(self.order.len());
            self.order.push(Some(index));
            self.in_map += 1;
            if !is_notice(&entry.message) {
                self.turns_in_map += 1;
                if index < self.mark {
                    self.before_mark += 1;
                }
            }
        }
    }

    fn leave(&mut self, index: usize) {
        let Some(entry) = self.entries.get_mut(index) else {
            return;
        };
        let slot = entry.slot.take();
        let turn = !is_notice(&entry.message);
        if let Some(cell) = slot.and_then(|slot| self.order.get_mut(slot)) {
            *cell = None;
            self.in_map = self.in_map.saturating_sub(1);
            if turn {
                self.turns_in_map = self.turns_in_map.saturating_sub(1);
                if index < self.mark {
                    self.before_mark = self.before_mark.saturating_sub(1);
                }
            }
        }
    }

    /// Copies recorded from here on are later than every message now in the map.
    fn set_mark(&mut self) {
        self.mark = self.entries.len();
        self.before_mark = self.turns_in_map;
    }

    /// Drop trailing gaps, and rebuild `order` once gaps outnumber the messages in it.
    fn tidy(&mut self) {
        while matches!(self.order.last(), Some(None)) {
            self.order.pop();
        }
        if self.order.len() > 2 * self.in_map + 64 {
            self.compact_from(0);
        }
    }

    /// Close the gaps in `order` from `from` on, keeping its order.
    fn compact_from(&mut self, from: usize) {
        let moved: Vec<usize> = self.order.drain(from..).flatten().collect();
        for index in moved {
            if let Some(entry) = self.entries.get_mut(index) {
                entry.slot = Some(self.order.len());
                self.order.push(Some(index));
            }
        }
    }

    fn slot_of(&self, index: usize) -> Option<usize> {
        self.entries.get(index).and_then(|entry| entry.slot)
    }

    fn live_index(&self, id: &str) -> Option<usize> {
        self.by_id
            .get(id)
            .copied()
            .filter(|index| self.slot_of(*index).is_some())
    }

    fn entered_now(&self, index: usize) -> bool {
        self.entries
            .get(index)
            .is_some_and(|entry| entry.entered == self.epoch)
    }

    /// `$rewindTo`: the message and every later one in the map; an unknown id clears the map.
    /// True when it was the person's `/rewind`, not the conversion's or a decline's rollback.
    fn rewind(&mut self, target: &str, converting: bool) -> bool {
        let from = self
            .live_index(target)
            .and_then(|index| self.slot_of(index))
            .unwrap_or(0);
        let undone: Vec<usize> = self
            .order
            .get(from..)
            .into_iter()
            .flatten()
            .flatten()
            .copied()
            .collect();
        let kept = (converting && self.only_conversion_skips(&undone)) || self.declines(&undone);
        if undone.iter().any(|index| self.is_turn(*index)) {
            self.removal_epoch = Some(self.epoch);
        }
        if kept {
            for index in &undone {
                self.leave(*index);
            }
            let keys: Vec<Option<CopyKey>> =
                undone.iter().map(|index| self.copy_key(*index)).collect();
            self.mark_dropped(&undone, &keys);
        } else {
            let deleted = self.undo(undone);
            self.undo_answers(&deleted);
        }
        self.tidy();
        self.set_mark();
        !kept
    }

    /// Messages, at least one, that upstream's conversion leaves out of the rebuilt history: an
    /// env-led coalesced copy is one, and following its links would undo the prompt in it.
    fn only_conversion_skips(&self, indices: &[usize]) -> bool {
        !indices.is_empty()
            && indices.iter().all(|index| {
                self.entries
                    .get(*index)
                    .is_some_and(|entry| conversion_skips(&entry.message))
            })
    }

    /// A pure rollback that is a declined tool's, not a person's `/rewind` or an abort: with every
    /// declinable call cancelled, useGeminiStream records 'Request cancelled.' and then sets the
    /// history back to its length before the prompt (2105-2147 at fb972b2). Only an empty context
    /// makes that a rollback rather than a rewrite. So past notices it ends on the declined turn,
    /// removes only messages that entered the map since the previous removal, and leaves nothing
    /// but notices. When compression shortened the history in the same turn upstream skips it
    /// (2135), and the declined turn can later end a person's `/rewind`: the last two conditions
    /// tell that apart.
    fn declines(&self, removed: &[usize]) -> bool {
        let turns: Vec<usize> = removed
            .iter()
            .copied()
            .filter(|index| self.is_turn(*index))
            .collect();
        let since = |index: &usize| {
            let entered = self.entries.get(*index).map(|entry| entry.entered);
            self.removal_epoch
                .is_none_or(|epoch| entered.is_some_and(|entered| entered > epoch))
        };
        turns.last().is_some_and(|index| self.declined(*index))
            && turns.iter().all(since)
            && self.turns_in_map == turns.len()
    }

    /// Any message but a notice.
    fn is_turn(&self, index: usize) -> bool {
        self.entries
            .get(index)
            .is_some_and(|entry| !is_notice(&entry.message))
    }

    /// A model turn whose tool calls the person declined: every declinable one cancelled, or
    /// every one. `isTopicTool` (125-126): the person never gets to decline that one.
    fn declined(&self, index: usize) -> bool {
        let Some(message) = self.entries.get(index).map(|entry| &entry.message) else {
            return false;
        };
        let calls = match (message.get("type"), message.get("toolCalls")) {
            (Some(Value::String(kind)), Some(Value::Array(calls))) if kind == "gemini" => calls,
            _ => return false,
        };
        let cancelled =
            |call: &&Value| call.get("status").and_then(Value::as_str) == Some("cancelled");
        let topic = |call: &&Value| {
            let name = call.get("name").and_then(Value::as_str);
            matches!(name, Some("update_topic" | "Update Topic Context"))
        };
        let mut declinable = calls.iter().filter(|call| !topic(call)).peekable();
        let all_declinable = declinable.peek().is_some() && declinable.all(|call| cancelled(&call));
        all_declinable || (!calls.is_empty() && calls.iter().all(|call| cancelled(&call)))
    }

    /// Delete entries and, through each copy, the originals it re-recorded; returns those deleted.
    fn undo(&mut self, mut pending: Vec<usize>) -> Vec<usize> {
        let mut deleted = Vec::new();
        while let Some(index) = pending.pop() {
            let Some(entry) = self.entries.get_mut(index) else {
                continue;
            };
            if entry.state == State::Deleted {
                continue;
            }
            entry.state = State::Deleted;
            pending.extend_from_slice(&entry.originals);
            self.leave(index);
            deleted.push(index);
        }
        deleted
    }

    /// A person's `/rewind` undoes a prompt's whole exchange: also the model turns recorded after
    /// its row, before the next row, that a rewrite left out of the map, copied or not. A declined
    /// later round's (2105-2147 at fb972b2: the rollback ends at the last tool result sent) and an
    /// Esc'd tool's calls record (chatRecordingService.ts 1133-1157) are such turns. Copies there
    /// repeat other exchanges, so they stay; a copy of one of these turns is out of the map too, as
    /// the map keeps a prompt's copy before its answer's. The work stays linear: a row is deleted
    /// once, its scan stops at the next row, and as rows never appear later (see `Entry::row`) and
    /// pairing never clears a deleted one, no two scans cross the same entry.
    fn undo_answers(&mut self, deleted: &[usize]) {
        let mut answers = Vec::new();
        for row in deleted {
            if !self.entries.get(*row).is_some_and(|entry| entry.row) {
                continue;
            }
            for (index, entry) in self.entries.iter().enumerate().skip(row + 1) {
                if entry.row {
                    break;
                }
                let gemini = entry.message.get("type").and_then(Value::as_str) == Some("gemini");
                if gemini && entry.originals.is_empty() && entry.state == State::Dropped {
                    answers.push(index);
                }
            }
        }
        self.undo(answers);
    }

    fn patch(&mut self, patch: &Value, converting: bool) {
        if patch.get("id").is_some_and(Value::is_string) {
            self.update(patch);
        }
        if let Some(Value::Array(updates)) = patch.get("updates") {
            for update in updates {
                if update.get("id").is_some_and(Value::is_string) {
                    self.update(update);
                }
            }
        }
        let ids = |field: &str| -> Vec<usize> {
            let listed = patch.get(field).and_then(Value::as_array);
            listed
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .filter_map(|id| self.live_index(id))
                .collect()
        };
        let removed = ids("removeIds");
        // upstream moves listed messages to the end in list order; unlisted ones keep theirs
        let order = ids("orderIds");
        self.resync(removed, order, converting);
    }

    /// A `$set.messages` history checkpoint (gemini-cli up to 361b0bb; `$patch` replaced it in
    /// d1cc08a): upstream rebuilds its map from the list, so unlisted messages leave it.
    fn checkpoint(&mut self, messages: &[Value], converting: bool) {
        let first_new = self.entries.len();
        let mut listed: Vec<usize> = Vec::new();
        for message in messages {
            self.seen += 1;
            let (Some(Value::String(id)), None) = (message.get("id"), message.get("$patch")) else {
                self.non_message += 1;
                continue;
            };
            if self.live_index(id).is_some_and(|index| index < first_new) {
                self.update(message);
                self.replay += 1;
            } else {
                self.put(id.clone(), message.clone());
            }
            listed.extend(self.live_index(id));
        }
        let kept: HashSet<usize> = listed.iter().copied().collect();
        let removed: Vec<usize> = self
            .order
            .iter()
            .flatten()
            .copied()
            .filter(|index| !kept.contains(index))
            .collect();
        self.resync(removed, listed, converting);
    }

    /// Remove `removed` from the map and move `order` to its end, as a tail rollback or a
    /// context rewrite (see `Fold`).
    fn resync(&mut self, removed: Vec<usize>, order: Vec<usize>, converting: bool) {
        let mut removed: Vec<(usize, usize)> = removed
            .into_iter()
            .filter_map(|index| self.slot_of(index).map(|slot| (slot, index)))
            .collect();
        removed.sort_unstable();
        removed.dedup();
        let removed: Vec<usize> = removed.into_iter().map(|(_, index)| index).collect();
        let gone: HashSet<usize> = removed.iter().copied().collect();
        let mut tail: HashSet<usize> = HashSet::new();
        let converted = converting && self.only_conversion_skips(&removed);
        let mut copies: Vec<(usize, usize)> = Vec::new();
        if !removed.is_empty() && !converted {
            // walked back past what this re-sync recorded, the map ends with its rolled-back tail
            let mut from = self.order.len();
            let mut gaps = false;
            while tail.len() < gone.len() {
                let Some(slot) = from.checked_sub(1) else {
                    break;
                };
                match self.order.get(slot).copied().flatten() {
                    None => gaps = true,
                    Some(index) if self.entered_now(index) => {}
                    Some(index) if gone.contains(&index) => {
                        tail.insert(index);
                    }
                    Some(_) => break,
                }
                from = slot;
            }
            // each gap is crossed once: one-in-one-out re-syncs would otherwise walk them all again
            if gaps {
                self.compact_from(from);
            }
            if self.declines(&removed) {
                tail.clear();
            }
        }
        if let Some(last) = removed.last().and_then(|index| self.slot_of(*index)) {
            for index in self.mark..self.entries.len() {
                let Some(entry) = self.entries.get(index) else {
                    continue;
                };
                // a notice is never a copy, also when an older recorder wrote it after this one
                // took the context over (auto-compression's, see `is_notice`)
                let copy = entry.originals.is_empty()
                    && !gone.contains(&index)
                    && !is_notice(&entry.message);
                if let Some(slot) = entry.slot.filter(|slot| *slot > last).filter(|_| copy) {
                    copies.push((slot, index));
                }
            }
            copies.sort_unstable();
        }
        for index in &removed {
            self.leave(*index);
        }
        let mut moved = HashSet::new();
        for index in order {
            if self.slot_of(index).is_some() && moved.insert(index) {
                self.leave(index);
                self.enter(index);
            }
        }
        self.tidy();
        if !removed.is_empty() {
            if removed.iter().any(|index| self.is_turn(*index)) {
                self.removal_epoch = Some(self.epoch);
            }
            let last = self.order.iter().rev().flatten().next().copied();
            let mut dropped = removed;
            if !tail.is_empty() && !last.is_some_and(|index| self.entered_now(index)) {
                dropped.retain(|index| !tail.contains(index));
                self.undo(tail.into_iter().collect());
            }
            let copies: Vec<usize> = copies.into_iter().map(|(_, index)| index).collect();
            // a copy never repeats a notice, and one between two user turns would split their run
            let (turns, notices): (Vec<usize>, Vec<usize>) =
                dropped.into_iter().partition(|index| self.is_turn(*index));
            let keys: Vec<Option<CopyKey>> =
                turns.iter().map(|index| self.copy_key(*index)).collect();
            let whole = self.before_mark == 0;
            self.pair(&turns, &keys, &copies, whole);
            self.mark_dropped(&turns, &keys);
            self.mark_dropped(&notices, &[]);
            self.tidy();
        }
        self.set_mark();
    }

    /// Mark messages already out of the map dropped, and pool them, by `keys` where given, for
    /// later whole rewrites.
    fn mark_dropped(&mut self, dropped: &[usize], keys: &[Option<CopyKey>]) {
        for (position, index) in dropped.iter().enumerate() {
            if let Some(entry) = self.entries.get_mut(*index) {
                entry.state = State::Dropped;
            }
            if let Some(Some(key)) = keys.get(position) {
                self.dropped
                    .entry(key_hash(key))
                    .or_default()
                    .insert(*index);
            }
        }
    }

    /// Pair copies with the removed messages they repeat, order-preserving. Compression copies a
    /// suffix after its snapshot, paired from the back. A rewrite that re-records the context from
    /// its first message (masking, truncation, `/chat resume`) pairs from the front, and when it
    /// re-records the whole context a copy of nothing removed may repeat an earlier dropped message.
    /// `getHistory()` coalesces consecutive turns of a role for Gemini 2 and 3 models, so one copy
    /// can repeat a run of them: user turns (the environment and the first prompt, a cancelled
    /// tool's result and the next prompt, IDE context and its prompt), and model turns where
    /// upstream's conversion dropped the user turn between them (see `conversion_skips`).
    fn pair(
        &mut self,
        removed: &[usize],
        removed_keys: &[Option<CopyKey>],
        copies: &[usize],
        whole: bool,
    ) {
        if copies.is_empty() {
            return;
        }
        let copy_keys: Vec<Option<CopyKey>> = copies.iter().map(|i| self.copy_key(*i)).collect();
        let mut at: HashMap<&CopyKey, Vec<usize>> = HashMap::new();
        for (position, key) in removed_keys.iter().enumerate() {
            if let Some(key) = key {
                at.entry(key).or_default().push(position);
            }
        }
        let part_keys = |index: &usize| -> Vec<PartKey> {
            let message = self.entries.get(*index).map(|entry| &entry.message);
            message.map(part_keys).unwrap_or_default()
        };
        let removed_parts: Vec<Vec<PartKey>> = removed.iter().map(part_keys).collect();
        let copy_parts: Vec<Vec<PartKey>> = copies.iter().map(part_keys).collect();
        let stale: Vec<bool> = removed
            .iter()
            .map(|index| {
                let message = self.entries.get(*index).map(|entry| &entry.message);
                message.is_some_and(conversion_skips)
            })
            .collect();
        let mut starts: HashMap<&PartKey, Vec<usize>> = HashMap::new();
        let mut ends: HashMap<&PartKey, Vec<usize>> = HashMap::new();
        for (position, parts) in removed_parts.iter().enumerate() {
            if let (Some(first), Some(last)) = (parts.first(), parts.last()) {
                starts.entry(first).or_default().push(position);
                ends.entry(last).or_default().push(position);
            }
        }
        let wanted = |slot: usize| copy_parts.get(slot).map(Vec::as_slice).unwrap_or_default();
        let opens = matches!(
            (copy_keys.first(), removed_keys.first()),
            (Some(Some(copy)), Some(Some(first))) if copy == first
        ) || Self::run(wanted(0), &removed_parts, &stale, &starts, 0, true)
            .is_some_and(|(start, _)| start == 0);
        // a whole rewrite re-records the context from its start, but a save made after `/rewind`
        // or `--resume` lacks the environment message, so judge by the first copy past it
        let lead = copy_keys.iter().enumerate().find_map(|(slot, key)| {
            let context = |(kind, text, _): &CopyKey| kind == "user" && is_injected_context(text);
            key.as_ref()
                .filter(|key| !context(key))
                .map(|key| (key, slot))
        });
        let front = opens
            || (whole
                && lead.is_some_and(|(key, slot)| {
                    at.contains_key(key)
                        || Self::run(wanted(slot), &removed_parts, &stale, &starts, 0, true)
                            .is_some()
                        || self.dropped_original(key, 0).is_some()
                }));
        let mut originals: Vec<Vec<usize>> = vec![Vec::new(); copies.len()];
        let mut order: Vec<usize> = (0..copies.len()).collect();
        if !front {
            order.reverse();
        }
        let edges = if front { &starts } else { &ends };
        let mut bound = if front { 0 } else { removed.len() };
        let mut dropped_bound = 0;
        for slot in order {
            let Some(Some(key)) = copy_keys.get(slot) else {
                continue;
            };
            let positions = at.get(key).map(Vec::as_slice).unwrap_or_default();
            let found = if front {
                positions.get(positions.partition_point(|p| *p < bound))
            } else {
                let below = positions.partition_point(|p| *p < bound);
                below.checked_sub(1).and_then(|p| positions.get(p))
            };
            let run = Self::run(wanted(slot), &removed_parts, &stale, edges, bound, front);
            // the nearer of the two in pairing order
            let span = match (found.copied(), run) {
                (Some(at), Some((start, end)))
                    if (front && at < start) || (!front && at >= end) =>
                {
                    Some((at, at + 1))
                }
                (_, Some(span)) => Some(span),
                (Some(at), None) => Some((at, at + 1)),
                (None, None) => None,
            };
            if let Some((start, end)) = span {
                bound = if front { end } else { start };
                if let Some(cell) = originals.get_mut(slot) {
                    *cell = removed.get(start..end).unwrap_or_default().to_vec();
                }
                continue;
            }
            // turns typed since the previous re-sync may sit among the copies of a partial rewrite
            if !(front && whole) {
                continue;
            }
            if let Some(found) = self.dropped_original(key, dropped_bound) {
                dropped_bound = found + 1;
                if let Some(original) = originals.get_mut(slot) {
                    original.push(found);
                }
            }
        }
        for (copy, found) in copies.iter().zip(originals) {
            if let Some(entry) = self.entries.get_mut(*copy) {
                entry.row &= found.is_empty();
                entry.originals = found;
            }
        }
    }

    /// The run of two or more consecutive removed turns whose parts, one after another, are the
    /// copy's (`want`): the one starting at or after `bound` from the front, else the one ending
    /// before it. `edges` maps each removed turn's first (front) or last part to positions; every
    /// part is keyed once per `pair`, so a comparison never re-reads its text. A `stale` message,
    /// one upstream's conversion leaves out, and a turn without parts to key (thought-only, like a
    /// binary read's acknowledgement) can sit inside the run without being in the copy.
    fn run(
        want: &[PartKey],
        removed_parts: &[Vec<PartKey>],
        stale: &[bool],
        edges: &HashMap<&PartKey, Vec<usize>>,
        bound: usize,
        front: bool,
    ) -> Option<(usize, usize)> {
        if want.len() < 2 {
            return None;
        }
        let positions = edges.get(if front { want.first() } else { want.last() }?)?;
        let below = positions.partition_point(|p| *p < bound);
        let start = *positions.get(if front { below } else { below.checked_sub(1)? })?;
        // how many parts the removed turn at `at` covers next, if it fits there; comparisons
        // stop at a few per part of the copy, so a crafted run of repeats stays linear
        let budget = std::cell::Cell::new(4 * want.len());
        let fits = |at: usize, taken: usize| -> Option<usize> {
            let parts = removed_parts.get(at).filter(|parts| !parts.is_empty())?;
            let range = if front {
                taken..taken + parts.len()
            } else {
                want.len().checked_sub(taken + parts.len())?..want.len() - taken
            };
            let wanted = want.get(range)?;
            budget.set(budget.get().checked_sub(wanted.len())?);
            (wanted == parts.as_slice()).then_some(parts.len())
        };
        let (mut next, mut edge, mut taken, mut members) = (Some(start), start, 0, 0);
        let step = |at: usize| {
            if front {
                at.checked_add(1)
            } else {
                at.checked_sub(1)
            }
        };
        let mut fitted = start;
        while taken < want.len() {
            if let Some(len) = next.and_then(|at| fits(at, taken)) {
                edge = next?;
                fitted = edge;
                next = step(edge);
                taken += len;
            } else if let Some(len) = (members > 0).then(|| fits(fitted, taken)).flatten() {
                // `--resume` re-derives a tool result the file also holds, under the same id:
                // the history carries that message twice and upstream's map once
                taken += len;
            } else if members > 0
                && next.is_some_and(|at| {
                    stale.get(at) == Some(&true) || removed_parts.get(at).is_some_and(Vec::is_empty)
                })
            {
                // the map can keep a stale one after a conversion when the file never re-synced
                // (the `$set.messages` recorder writes none when the count comes out the same);
                // `stripThoughts` drops a turn left without parts (geminiChat.ts 1841-1880)
                budget.set(budget.get().checked_sub(1)?);
                edge = next?;
                next = step(edge);
                continue;
            } else {
                return None;
            }
            members += 1;
        }
        if members < 2 {
            return None;
        }
        Some(if front {
            (start, edge + 1)
        } else {
            (edge, start + 1)
        })
    }

    /// The oldest dropped entry at or after `bound` that `key` repeats, pruning entries that are
    /// no longer dropped, or repeat a turn since undone through another copy of it.
    fn dropped_original(&mut self, key: &CopyKey, bound: usize) -> Option<usize> {
        let hash = key_hash(key);
        let mut cursor = bound;
        loop {
            let index = *self.dropped.get(&hash)?.range(cursor..).next()?;
            let usable = self.entries.get(index).is_some_and(|entry| {
                let deleted = |original: &usize| {
                    self.entries
                        .get(*original)
                        .is_none_or(|entry| entry.state == State::Deleted)
                };
                entry.state == State::Dropped && !entry.originals.iter().any(deleted)
            });
            if !usable {
                if let Some(set) = self.dropped.get_mut(&hash) {
                    set.remove(&index);
                }
                continue;
            }
            if self.copy_key(index).as_ref() == Some(key) {
                return Some(index);
            }
            cursor = index + 1;
        }
    }

    /// None for the interrupted-turn closer: the CLI writes it the first time inside a rewrite,
    /// where it would take the place of an earlier closer and misalign every pair after it. A
    /// coalesced user copy keys past its leading injected context (see `past_injected`).
    fn copy_key(&self, index: usize) -> Option<CopyKey> {
        let message = &self.entries.get(index)?.message;
        let kind = history_kind(message);
        let mut parts = parts_of(message.get("content").unwrap_or(&Value::Null));
        let calls = message.get("toolCalls").and_then(Value::as_array);
        let mut tools: Vec<(String, String)> =
            calls.into_iter().flatten().map(name_and_id).collect();
        tools.extend(parts.iter().filter_map(part_tool));
        tools.sort_unstable();
        tools.dedup();
        if kind == "user" {
            parts = past_injected(parts);
        }
        let text = visible_text(&parts_text(parts));
        if tools.is_empty() && text == INTERRUPTED_PLACEHOLDER {
            return None;
        }
        Some((kind.to_string(), text, tools))
    }

    /// Upstream `applySinglePatch` on a message in the map: replace `content`; give gemini tool
    /// calls a result by id, only where none was recorded. Upstream records a model turn's reply
    /// cleaned (`responseText`, geminiChat.ts 1547-1659 at fb972b2), and a re-sync writes back the
    /// history's raw parts (chatRecordingService.ts 1364-1381): when those read the same (see
    /// `visible_text`), the turn keeps the reply it recorded, among the parts' calls (see
    /// `with_recorded_reply`). A turn without one keeps its content: after Esc its calls have a
    /// record of their own (1133-1157), which a copy of the turn then pairs with.
    fn update(&mut self, patch: &Value) {
        let Some(index) = patch
            .get("id")
            .and_then(Value::as_str)
            .and_then(|id| self.live_index(id))
        else {
            return;
        };
        let Some(Entry {
            message: Value::Object(message),
            reply,
            ..
        }) = self.entries.get_mut(index)
        else {
            return;
        };
        if let Some(content) = patch.get("content") {
            let gemini = message.get("type").and_then(Value::as_str) == Some("gemini");
            let visible = |content: &Value| visible_text(&part_text(content));
            let kept = gemini && {
                let new = visible(content);
                let old = reply
                    .get_or_insert_with(|| message.get("content").map(visible).unwrap_or_default());
                let same = *old == new;
                *old = new;
                same
            };
            if !kept {
                message.insert("content".to_string(), content.clone());
            } else if reply.as_ref().is_some_and(|reply| !reply.is_empty())
                && parts_of(content)
                    .iter()
                    .any(|part| part.get("functionCall").is_some())
            {
                let slot = message.entry("content").or_insert(Value::Null);
                // a copy's parts already split its prose as the history does, which pairing keys
                let same = slot.is_array()
                    && (parts_of(slot).iter().filter_map(prose))
                        .eq(parts_of(content).iter().filter_map(prose));
                if same {
                    *slot = content.clone();
                } else {
                    let recorded = recorded_text(std::mem::take(slot));
                    *slot = with_recorded_reply(content, recorded);
                }
            }
        }
        let Some(Value::Array(calls)) = patch.get("toolCalls") else {
            return;
        };
        if message.get("type").and_then(Value::as_str) != Some("gemini") {
            return;
        }
        let Some(Value::Array(existing)) = message.get_mut("toolCalls") else {
            return;
        };
        for call in calls.iter().filter(|call| call.is_object()) {
            let id = call.get("id");
            let target = existing.iter_mut().find(|tc| tc.get("id") == id);
            if let (Some(result), Some(Value::Object(target))) = (call.get("result"), target) {
                if target.get("result").is_none_or(Value::is_null) {
                    target.insert("result".to_string(), result.clone());
                }
            }
        }
    }

    /// Upstream `finalize` refuses a history without both ids and falls back to legacy JSON.
    fn complete(&self) -> bool {
        self.session().is_some() && self.project_hash.as_ref().is_some_and(js_truthy)
    }

    fn session(&self) -> Option<String> {
        self.session_id
            .as_ref()
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string)
    }

    fn record(&self, tally: &Tally) {
        let undone = self
            .entries
            .iter()
            .filter(|entry| entry.state == State::Deleted)
            .count();
        tally.seen_n(self.seen);
        tally.skip_n(Skip::Meta, self.meta);
        tally.skip_n(Skip::NonMessage, self.non_message);
        tally.skip_n(Skip::Replay, self.replay);
        tally.skip_n(Skip::Unreferenced, undone as u64);
        let sample = self.first_error.as_deref().unwrap_or("");
        for _ in 0..self.errors {
            tally.error(sample);
        }
    }

    /// The transcript in order, each message flagged when it only repeats another.
    fn messages(&self) -> impl Iterator<Item = (&Value, bool)> {
        self.entries
            .iter()
            .filter(|entry| entry.state != State::Deleted)
            .map(|entry| (&entry.message, !entry.originals.is_empty()))
    }
}

/// `Undone` when the read is empty because the file itself undid what it held: rolled back or
/// rewound in records the read saw whole. Upstream only appends complete lines, so an error-free
/// read ending on one is the session as it stood; a torn or foreign read never qualifies.
fn parse_jsonl(data: &str, path: &Path, tally: &Tally) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    let fold = Fold::read(data);
    if !fold.complete() {
        if let Ok(root) = serde_json::from_str::<Value>(data) {
            if root.get("sessionId").is_some() {
                let (messages, events) = parse_document(&root, path, tally);
                return (messages, events, ReadOutcome::Complete);
            }
        }
    }
    fold.record(tally);
    let session = fold.session().unwrap_or_else(|| file_stem(path));
    let (messages, events) = emit_messages(&session, fold.messages(), tally);
    let undone = messages.is_empty()
        && events.is_empty()
        && fold.complete()
        && fold.errors == 0
        && data.ends_with('\n')
        && fold
            .entries
            .iter()
            .any(|entry| entry.state == State::Deleted);
    let outcome = if undone {
        ReadOutcome::Undone
    } else {
        ReadOutcome::Complete
    };
    (messages, events, outcome)
}

fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    parse_with_tally(path, &crate::intake::file("gemini", path))
}

fn parse_with_tally(path: &Path, tally: &Tally) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!(
                "  ! gemini: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            );
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (Vec::new(), Vec::new(), ReadOutcome::Skipped);
        }
    };
    if path.extension().and_then(|x| x.to_str()) == Some("jsonl") {
        return parse_jsonl(&data, path, tally);
    }
    match serde_json::from_str::<Value>(&data) {
        Ok(root) => {
            let (messages, events) = parse_document(&root, path, tally);
            (messages, events, ReadOutcome::Complete)
        }
        Err(e) => {
            // whole-file failure: the file itself is the one seen-and-errored record
            tally.seen();
            tally.error(&format!("{e}: {}", crate::intake::clip(&data, 80)));
            (Vec::new(), Vec::new(), ReadOutcome::Complete)
        }
    }
}

/// A main-session file name. Subagent chats nest as `chats/<parent>/<id>.jsonl` and activity
/// logs live at `logs/session-<id>.jsonl`; neither is a top-level conversation here.
fn is_session_name(name: &str) -> bool {
    name.starts_with("session-") && (name.ends_with(".json") || name.ends_with(".jsonl"))
}

/// A legacy `X.json` with a plain `X.jsonl` beside it: resuming migrated the whole record
/// into the `.jsonl`, which carries the conversation from then on.
fn superseded(path: &Path) -> bool {
    if path.extension().and_then(|x| x.to_str()) != Some("json") {
        return false;
    }
    let mut sibling = path.as_os_str().to_os_string();
    sibling.push("l");
    fs::symlink_metadata(&sibling).is_ok_and(|meta| meta.is_file() && !metadata_is_link(&meta))
}

/// Walk every `~/.gemini/tmp/<projectHash>/chats/session-*.json(l)` and collect the user's
/// Gemini CLI turns + tool events, returning superseded legacy files separately so the cache
/// forgets them. Project attribution is the constant "gemini": the store keys sessions by an
/// opaque SHA256 projectHash with no path recorded, so unlike the other adapters there is no
/// readable working dir to bucket by (a hash->path map is a follow-up).
fn session_files(root: &Path) -> (Vec<PathBuf>, HashSet<PathBuf>) {
    let mut files: Vec<PathBuf> = Vec::new();
    let mut retired: HashSet<PathBuf> = HashSet::new();
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("gemini", root, "source root is not a plain directory");
            return (files, retired);
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return (files, retired),
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            return (files, retired);
        }
    }
    let dirs = match fs::read_dir(root) {
        Ok(dirs) => dirs,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            return (files, retired);
        }
    };
    for entry in dirs {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", root, &error);
                continue;
            }
        };
        let project = entry.path();
        match plain_entry_metadata(&entry) {
            Ok(Some(meta)) if meta.is_dir() => {}
            Ok(None) => {
                crate::ingest::warn_source_skip(
                    "gemini",
                    &project,
                    "symlink sources are not followed",
                );
                continue;
            }
            Ok(Some(_)) => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", &project, &error);
                continue;
            }
        }
        let chats = project.join("chats");
        match fs::symlink_metadata(&chats) {
            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
            Ok(meta) if metadata_is_link(&meta) => {
                crate::ingest::warn_source_skip(
                    "gemini",
                    &chats,
                    "symlink sources are not followed",
                );
                continue;
            }
            Ok(_) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", &chats, &error);
                continue;
            }
        }
        let rd = match fs::read_dir(&chats) {
            Ok(rd) => rd,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", &chats, &error);
                continue;
            }
        };
        for file in rd {
            let file = match file {
                Ok(file) => file,
                Err(error) => {
                    crate::ingest::warn_source_skip("gemini", &chats, &error);
                    continue;
                }
            };
            let path = file.path();
            match plain_entry_metadata(&file) {
                Ok(Some(meta)) if meta.is_file() => {}
                Ok(None) => {
                    crate::ingest::warn_source_skip(
                        "gemini",
                        &path,
                        "symlink sources are not followed",
                    );
                    continue;
                }
                Ok(Some(_)) => continue,
                Err(error) => {
                    crate::ingest::warn_source_skip("gemini", &path, &error);
                    continue;
                }
            }
            let name = path
                .file_name()
                .map(|name| name.to_string_lossy())
                .unwrap_or_default();
            if !is_session_name(&name) {
                continue;
            }
            if superseded(&path) {
                retired.insert(path);
            } else {
                files.push(path);
            }
        }
    }
    (files, retired)
}

pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".gemini").join("tmp");
    let (files, retired) = session_files(&root);
    let pass = crate::ingest_cache::collect_cached_retiring_for(
        cache, "gemini", &root, &files, &retired, parse_file,
    );
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). File store -> Stat.
pub struct Gemini;
impl crate::ingest::registry::Adapter for Gemini {
    fn name(&self) -> &'static str {
        "gemini"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".gemini").join("tmp")]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        // chats/session-*.json(l) is the conversation; logs/ and tool-outputs/ are telemetry
        path.file_name()
            .and_then(|x| x.to_str())
            .is_some_and(is_session_name)
            && path
                .parent()
                .and_then(Path::file_name)
                .is_some_and(|dir| dir == "chats")
            && !superseded(path)
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_with_tally, session_files, visible_text, Gemini};
    use crate::ingest::registry::Adapter;
    use crate::ingest_cache::ReadOutcome;
    use crate::intake::{Skip, Tally};
    use std::path::{Path, PathBuf};

    fn temp_root(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "agrep-gemini-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    fn parse(path: &Path) -> (Vec<crate::model::Message>, Vec<crate::model::Event>, Tally) {
        let tally = Tally::default();
        let (messages, events, outcome) = parse_with_tally(path, &tally);
        let undone = outcome == ReadOutcome::Undone && messages.is_empty() && events.is_empty();
        assert!(outcome == ReadOutcome::Complete || undone, "{outcome:?}");
        (messages, events, tally)
    }

    /// (seen, rows, agent_rows, Σskips over every kind, errors): the audit identity's terms.
    fn identity(tally: &Tally) -> (u64, u64, u64, u64, u64) {
        let (seen, rows, agent_rows, _, errors) = tally.test_record_counts(Skip::Meta);
        let skips = Skip::ALL
            .iter()
            .map(|skip| tally.test_record_counts(*skip).3)
            .sum();
        (seen, rows, agent_rows, skips, errors)
    }

    #[test]
    fn jsonl_folds_last_writes_rewinds_and_patches_without_dropping_turns() {
        let root = temp_root("fold");
        let path = root.join("session-2026-04-20T09-00-6a6a6a6a.jsonl");
        let lines = [
            r#"{"sessionId":"6a6a6a6a-0000-4000-8000-000000000001","projectHash":"h","startTime":"2026-04-20T09:00:00.000Z","lastUpdated":"2026-04-20T09:00:00.000Z","kind":"main"}"#,
            r#"{"id":"env","timestamp":"2026-04-20T09:00:00.500Z","type":"user","content":[{"text":"<session_context>\nThis is the Gemini CLI.\n</session_context>"}]}"#,
            r#"{"id":"u1","timestamp":"2026-04-20T09:00:01.000Z","type":"user","content":[{"text":"port the "},{"text":"env loader"},{"inlineData":{"mimeType":"image/png","data":"AA=="}}]}"#,
            r#"{"$set":{"lastUpdated":"2026-04-20T09:00:01.000Z"}}"#,
            r#"{"id":"g1","timestamp":"2026-04-20T09:00:02.000Z","type":"gemini","content":"","model":"gemini-3-pro-preview","thoughts":[{"subject":"plan","description":"hidden"}]}"#,
            r#"{"id":"g1","timestamp":"2026-04-20T09:00:02.000Z","type":"gemini","content":"","model":"gemini-3-pro-preview","toolCalls":[{"id":"todo-1","name":"write_todos","args":{"todos":[{"description":"Port the yaml loader","status":"pending"}]},"result":null,"status":"success"}]}"#,
            r#"{"id":"r1","timestamp":"2026-04-20T09:00:03.000Z","type":"user","content":[{"functionResponse":{"id":"todo-1","name":"write_todos","response":{"output":"ok"}}}]}"#,
            r#"{"id":"g2","timestamp":"2026-04-20T09:00:04.000Z","type":"gemini","content":"Ported the env loader.","model":"gemini-3-pro-preview"}"#,
            r#"{"id":"u2","timestamp":"2026-04-20T09:01:00.000Z","type":"user","content":[{"text":"undo that please"}]}"#,
            r#"{"id":"g3","timestamp":"2026-04-20T09:01:01.000Z","type":"gemini","content":"Undone.","model":"gemini-3-pro-preview"}"#,
            r#"{"$rewindTo":"u2"}"#,
            r#"{"$patch":{"updates":[{"id":"g1","toolCalls":[{"id":"todo-1","result":[{"functionResponse":{"id":"todo-1","name":"write_todos","response":{"output":"Updated 1 todo"}}}]}]},{"id":"g2","content":[{"text":"weighing it","thought":true},{"text":"Ported the env loader, tests pass."}]}],"removeIds":["r1"]}}"#,
            r#"{"id":"u3","timestamp":"2026-04-20T09:02:00.000Z","type":"user","content":[{"text":"@notes.md summarize"},{"text":"\n--- Content from referenced files ---\nexpanded file body"}],"displayContent":[{"text":"@notes.md summarize"}]}"#,
            r#"{"id":"i1","timestamp":"2026-04-20T09:02:00.500Z","type":"info","content":"Request cancelled."}"#,
            r#"{"$patch":{"orderIds":["u3","i1","env"]}}"#,
            "{\"id\":\"torn\",\"type\":\"user\",\"content\":[{\"te",
        ];
        std::fs::write(&path, lines.join("\n")).unwrap();
        let (messages, events, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);

        let rows: Vec<(&str, &str, u32)> = messages
            .iter()
            .map(|m| (&*m.text, &*m.reply, m.turn))
            .collect();
        assert_eq!(
            rows,
            vec![
                (
                    "port the env loader",
                    "Ported the env loader, tests pass.",
                    0
                ),
                ("@notes.md summarize", "", 1),
            ]
        );
        assert!(messages
            .iter()
            .all(|m| &*m.session == "6a6a6a6a-0000-4000-8000-000000000001"));
        assert_eq!(&*messages[0].model, "gemini-3-pro-preview");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "write_todos");
        assert_eq!(events[0].call_id, "todo-1");
        assert_eq!(events[0].output, "Updated 1 todo");
        assert!(events[0].input.contains("Port the yaml loader"));
        // 16 lines: metadata, $set, $rewindTo, two $patch lines and the info notice are meta;
        // g1's first write is replayed; u2 and g3 (rewind) are unreferenced; r1 survives its
        // removeIds as a tool result; the preamble is a wrapper and the torn last line errs.
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!((seen, rows, agent_rows, errors), (16, 2, 2, 1));
        assert_eq!(seen, rows + agent_rows + skips + errors);
        assert_eq!(tally.test_record_counts(Skip::Meta).3, 6);
        assert_eq!(tally.test_record_counts(Skip::Replay).3, 1);
        assert_eq!(tally.test_record_counts(Skip::Unreferenced).3, 2);
        assert_eq!(tally.test_record_counts(Skip::NonHuman).3, 1);
        assert_eq!(tally.test_record_counts(Skip::Wrapper).3, 1);
    }

    #[test]
    fn rewind_to_an_unknown_id_clears_and_a_set_checkpoint_keeps_earlier_turns() {
        let root = temp_root("checkpoint");
        let path = root.join("session-2026-04-21T09-00-6b6b6b6b.jsonl");
        let lines = [
            r#"{"sessionId":"6b6b6b6b-0000-4000-8000-000000000002","projectHash":"h"}"#,
            r#"{"id":"a","timestamp":"2026-04-21T09:00:01.000Z","type":"user","content":"first draft"}"#,
            r#"{"$rewindTo":"never-recorded"}"#,
            r#"{"id":"b","timestamp":"2026-04-21T09:00:02.000Z","type":"user","content":"second draft"}"#,
            r#"{"$set":{"sessionId":"6b6b6b6b-0000-4000-8000-00000000000b","messages":[{"id":"c","timestamp":"2026-04-21T09:00:03.000Z","type":"user","content":[{"text":"checkpointed prompt"}]},{"no":"id"}]}}"#,
        ];
        std::fs::write(&path, lines.join("\r\n")).unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        let rows: Vec<(&str, &str, u32)> = messages
            .iter()
            .map(|m| (&*m.session, &*m.text, m.turn))
            .collect();
        let session = "6b6b6b6b-0000-4000-8000-00000000000b";
        assert_eq!(
            rows,
            vec![
                (session, "second draft", 0),
                (session, "checkpointed prompt", 1)
            ]
        );
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!((seen, rows, errors), (7, 2, 0));
        assert_eq!(tally.test_record_counts(Skip::Unreferenced).3, 1);
        assert_eq!(seen, rows + agent_rows + skips + errors);
    }

    #[test]
    fn legacy_documents_read_part_list_prompts_and_skip_tool_results() {
        let root = temp_root("parts");
        let path = root.join("session-2026-02-15T10-00-6c6c6c6c.json");
        std::fs::write(
            &path,
            r#"{"sessionId":"6c6c6c6c-0000-4000-8000-000000000003","projectHash":"h","messages":[
                {"id":"u1","timestamp":"2026-02-15T10:00:01.000Z","type":"user","content":[{"text":"rename the "},{"text":"config flag"}]},
                {"id":"g1","timestamp":"2026-02-15T10:00:02.000Z","type":"gemini","content":[{"text":"Renamed it."}],"model":"gemini-2.5-pro"},
                {"id":"r1","timestamp":"2026-02-15T10:00:03.000Z","type":"user","content":[{"functionResponse":{"name":"shell","response":{"output":"done"}}}]},
                {"id":"u2","timestamp":"2026-02-15T10:00:04.000Z","type":"user","content":[{"inlineData":{"mimeType":"image/png","data":"AA=="}}]}
            ]}"#,
        )
        .unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "rename the config flag");
        assert_eq!(&*messages[0].reply, "Renamed it.");
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!((seen, rows, agent_rows, errors), (4, 1, 1, 0));
        assert_eq!(tally.test_record_counts(Skip::NonHuman).3, 1);
        assert_eq!(tally.test_record_counts(Skip::EmptyText).3, 1);
        assert_eq!(seen, rows + agent_rows + skips + errors);
    }

    /// Sessions written by upstream's own recorder (crates/agrep-cli/tests/fixtures/gemini_compress).
    fn recorded(name: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../agrep-cli/tests/fixtures/gemini_compress/home/.gemini/tmp")
            .join("hash7777synthetic/chats")
            .join(name)
    }

    fn turns(messages: &[crate::model::Message]) -> Vec<(u32, &str, &str)> {
        messages
            .iter()
            .map(|m| (m.turn, &*m.who, m.text.lines().next().unwrap_or("")))
            .collect()
    }

    #[test]
    fn compression_keeps_compressed_turns_and_files_the_snapshot_as_a_recap() {
        let path = recorded("session-2026-04-14T09-00-c0c0c0c0.jsonl");
        let (messages, events, tally) = parse(&path);
        assert_eq!(
            turns(&messages),
            vec![
                (0, "user", "investigate the alpaca module"),
                (1, "user", "investigate the bison module"),
                (2, "user", "investigate the cheetah module"),
                (3, "user", "investigate the dingo module"),
                (4, "recap", "<state_snapshot>"),
                (5, "user", "now investigate the elephant module"),
            ]
        );
        // the dingo turn keeps its own moment, not the re-recorded copy's
        let dingo = crate::ingest::parse_timestamp::rfc3339(Some("2026-04-14T09:03:00.000Z"));
        assert_eq!(messages[3].ts, dingo);
        assert_eq!(&*messages[3].reply, "The dingo module has 412 lines.");
        let recap = &messages[4];
        assert!(recap.reply.is_empty() && recap.model.is_empty());
        assert_eq!(&*recap.model_source, "recap");
        let outputs: Vec<(&str, &str)> = events
            .iter()
            .map(|e| (e.call_id.as_str(), e.output.as_str()))
            .collect();
        assert_eq!(
            outputs,
            vec![
                ("read_file-1", "export const alpaca = 1;"),
                ("write_todos-1", "Successfully updated the todo list."),
                ("run_shell_command-1", "412 src/dingo.ts"),
            ]
        );
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!(seen, rows + agent_rows + skips + errors);
        // three tool-call rewrites plus the four re-recorded tail messages
        assert_eq!(tally.test_record_counts(Skip::Replay).3, 7);
        assert_eq!(tally.test_record_counts(Skip::Unreferenced).3, 0);
    }

    #[test]
    fn checkpoint_compression_from_the_august_recorder_keeps_its_turns() {
        let path = recorded("session-2026-08-20T09-00-a2a2a2a2.jsonl");
        let (messages, events, tally) = parse(&path);
        assert_eq!(
            turns(&messages),
            vec![
                (0, "user", "profile the kestrel cache"),
                (1, "user", "profile the lemur queue"),
                (2, "recap", "<state_snapshot>"),
                (3, "user", "profile the marmot scheduler"),
            ]
        );
        assert_eq!(&*messages[1].reply, "The lemur queue is unbounded.");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].output, "export class Lemur {}");
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!(seen, rows + agent_rows + skips + errors);
    }

    #[test]
    fn a_resumed_then_compressed_legacy_session_keeps_its_turns() {
        let (legacy, _, _) = parse(&recorded("session-2026-03-01T10-00-b1b1b1b1.json"));
        let (current, _, tally) = parse(&recorded("session-2026-03-01T10-00-b1b1b1b1.jsonl"));
        assert_eq!(
            turns(&current),
            vec![
                (0, "user", "audit the falcon parser"),
                (1, "user", "audit the gecko lexer"),
                (2, "user", "audit the heron emitter"),
                (3, "user", "audit the ibis printer"),
                (4, "recap", "<state_snapshot>"),
                (5, "user", "audit the jackal linker"),
            ]
        );
        let identity_of =
            |m: &crate::model::Message| (m.session.to_string(), m.turn, m.ts, m.text.to_string());
        let before: Vec<_> = legacy.iter().map(identity_of).collect();
        let after: Vec<_> = current.iter().take(3).map(identity_of).collect();
        assert_eq!(before, after);
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!(seen, rows + agent_rows + skips + errors);
    }

    #[test]
    fn rewinding_a_re_recorded_copy_takes_its_original() {
        let root = temp_root("rewind-copy");
        let path = root.join("session-2026-04-22T09-00-6f6f6f6f.jsonl");
        let lines = [
            r#"{"sessionId":"6f6f6f6f-0000-4000-8000-000000000006","projectHash":"h"}"#,
            r#"{"id":"a","timestamp":"2026-04-22T09:00:01.000Z","type":"user","content":[{"text":"alpha"}]}"#,
            r#"{"id":"ga","timestamp":"2026-04-22T09:00:02.000Z","type":"gemini","content":"A1"}"#,
            r#"{"id":"b","timestamp":"2026-04-22T09:01:01.000Z","type":"user","content":[{"text":"beta"}]}"#,
            r#"{"id":"gb","timestamp":"2026-04-22T09:01:02.000Z","type":"gemini","content":"B1"}"#,
            r#"{"$set":{"sessionId":"6f6f6f6f-0000-4000-8000-000000000006"}}"#,
            r#"{"id":"s","timestamp":"2026-04-22T09:02:00.000Z","type":"user","content":[{"text":"<state_snapshot>a and b</state_snapshot>"}]}"#,
            r#"{"id":"k","timestamp":"2026-04-22T09:02:00.000Z","type":"gemini","content":[{"text":"Got it. Thanks for the additional context!"}]}"#,
            r#"{"id":"b2","timestamp":"2026-04-22T09:02:00.000Z","type":"user","content":[{"text":"beta"}]}"#,
            r#"{"id":"gb2","timestamp":"2026-04-22T09:02:00.000Z","type":"gemini","content":[{"text":"B1"}]}"#,
            r#"{"$patch":{"removeIds":["a","ga","b","gb"]}}"#,
            r#"{"$set":{"lastUpdated":"2026-04-22T09:02:00.000Z"}}"#,
            r#"{"$rewindTo":"b2"}"#,
        ];
        std::fs::write(&path, lines.join("\n")).unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(
            turns(&messages),
            vec![
                (0, "user", "alpha"),
                (1, "recap", "<state_snapshot>a and b</state_snapshot>")
            ]
        );
        assert_eq!(&*messages[0].reply, "A1");
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!(seen, rows + agent_rows + skips + errors);
        assert_eq!(tally.test_record_counts(Skip::Unreferenced).3, 4);
    }

    #[test]
    fn a_prompt_retyped_between_two_rewrites_stays_its_own_turn() {
        let root = temp_root("retyped");
        let path = root.join("session-2026-04-23T09-00-6e6e6e6e.jsonl");
        let lines = [
            r#"{"sessionId":"6e6e6e6e-0000-4000-8000-000000000007","projectHash":"h"}"#,
            r#"{"id":"u1","timestamp":"2026-04-23T09:00:01.000Z","type":"user","content":[{"text":"continue"}]}"#,
            r#"{"id":"c1","timestamp":"2026-04-23T09:01:00.000Z","type":"user","content":[{"text":"continue"}]}"#,
            r#"{"$set":{"lastUpdated":"2026-04-23T09:01:00.000Z"}}"#,
            r#"{"$patch":{"removeIds":["u1"]}}"#,
            r#"{"id":"u2","timestamp":"2026-04-23T09:05:01.000Z","type":"user","content":[{"text":"continue"}]}"#,
            r#"{"id":"c2","timestamp":"2026-04-23T09:06:00.000Z","type":"user","content":[{"text":"continue"}]}"#,
            r#"{"id":"c3","timestamp":"2026-04-23T09:06:00.000Z","type":"user","content":[{"text":"continue"}]}"#,
            r#"{"$patch":{"removeIds":["c1","u2"]}}"#,
        ];
        std::fs::write(&path, lines.join("\n")).unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        let rows: Vec<(u32, i64)> = messages.iter().map(|m| (m.turn, m.ts)).collect();
        let at = |iso| crate::ingest::parse_timestamp::rfc3339(Some(iso));
        assert_eq!(
            rows,
            vec![
                (0, at("2026-04-23T09:00:01.000Z")),
                (1, at("2026-04-23T09:05:01.000Z"))
            ]
        );
        assert_eq!(tally.test_record_counts(Skip::Replay).3, 3);
    }

    /// The August recorder can fold the environment message's removal after `/rewind` into the
    /// checkpoint that rolls back the next, aborted, prompt: the aborted tail is undone all the same.
    #[test]
    fn an_aborted_tail_rolls_back_beside_an_earlier_removal() {
        let root = temp_root("split-tail");
        let path = root.join("session-2026-04-24T09-00-6f6f6f6f.jsonl");
        let lines = [
            r#"{"sessionId":"6f6f6f6f-0000-4000-8000-000000000008","projectHash":"h"}"#,
            r#"{"$set":{"messages":[{"id":"env","timestamp":"2026-04-24T09:00:00.000Z","type":"user","content":[{"text":"<session_context>\nx\n</session_context>"}]}]}}"#,
            r#"{"id":"u1","timestamp":"2026-04-24T09:00:01.000Z","type":"user","content":[{"text":"alpaca"}]}"#,
            r#"{"$set":{"lastUpdated":"2026-04-24T09:00:01.000Z"}}"#,
            r#"{"id":"g1","timestamp":"2026-04-24T09:00:02.000Z","type":"gemini","content":"ok alpaca"}"#,
            r#"{"$set":{"lastUpdated":"2026-04-24T09:00:02.000Z"}}"#,
            r#"{"id":"u2","timestamp":"2026-04-24T09:01:00.000Z","type":"user","content":[{"text":"aborted"}]}"#,
            r#"{"$set":{"lastUpdated":"2026-04-24T09:01:00.000Z"}}"#,
            r#"{"$set":{"messages":[{"id":"u1","timestamp":"2026-04-24T09:00:01.000Z","type":"user","content":[{"text":"alpaca"}]},{"id":"g1","timestamp":"2026-04-24T09:00:02.000Z","type":"gemini","content":"ok alpaca"}]}}"#,
            r#"{"id":"u3","timestamp":"2026-04-24T09:02:00.000Z","type":"user","content":[{"text":"cheetah"}]}"#,
        ];
        std::fs::write(&path, lines.join("\n")).unwrap();
        let (messages, _, _) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        let rows: Vec<(&str, &str)> = messages.iter().map(|m| (&*m.text, &*m.reply)).collect();
        assert_eq!(rows, vec![("alpaca", "ok alpaca"), ("cheetah", "")]);
    }

    /// `/rewind` to the environment message undoes it; a `/chat resume` after that still re-records
    /// the saved context from its start, so its copies pair with what they repeat from the front.
    #[test]
    fn a_chat_resume_after_rewinding_the_environment_pairs_from_the_front() {
        let root = temp_root("resume-after-env");
        let path = root.join("session-2026-04-24T09-00-6a6a6a6a.jsonl");
        let env =
            r#""type":"user","content":[{"text":"<session_context>\nx\n</session_context>"}]"#;
        let lines = [
            r#"{"sessionId":"6a6a6a6a-0000-4000-8000-00000000000b","projectHash":"h"}"#.to_string(),
            format!(r#"{{"id":"env","timestamp":"2026-04-24T09:00:00.000Z",{env}}}"#),
            r#"{"id":"u1","timestamp":"2026-04-24T09:01:00.000Z","type":"user","content":[{"text":"alpaca"}]}"#.to_string(),
            r#"{"id":"g1","timestamp":"2026-04-24T09:01:30.000Z","type":"gemini","content":"ok alpaca"}"#.to_string(),
            r#"{"$set":{"lastUpdated":"2026-04-24T09:01:30.000Z"}}"#.to_string(),
            format!(r#"{{"id":"env","timestamp":"2026-04-24T09:00:00.000Z",{env}}}"#),
            r#"{"id":"s1","timestamp":"2026-04-24T09:02:00.000Z","type":"user","content":[{"text":"<state_snapshot>\n<overall_goal>alpaca</overall_goal>\n</state_snapshot>"}]}"#.to_string(),
            r#"{"id":"a1","timestamp":"2026-04-24T09:02:00.000Z","type":"gemini","content":[{"text":"Got it. Thanks for the additional context!"}]}"#.to_string(),
            r#"{"$patch":{"removeIds":["u1","g1"]}}"#.to_string(),
            r#"{"$rewindTo":"env"}"#.to_string(),
            r#"{"id":"u2","timestamp":"2026-04-24T09:03:00.000Z","type":"user","content":[{"text":"bison"}]}"#.to_string(),
            r#"{"$set":{"lastUpdated":"2026-04-24T09:03:00.000Z"}}"#.to_string(),
            format!(r#"{{"id":"e3","timestamp":"2026-04-24T09:04:00.000Z",{env}}}"#),
            r#"{"id":"u3","timestamp":"2026-04-24T09:04:00.000Z","type":"user","content":[{"text":"alpaca"}]}"#.to_string(),
            r#"{"id":"g3","timestamp":"2026-04-24T09:04:00.000Z","type":"gemini","content":[{"text":"ok alpaca"}]}"#.to_string(),
            r#"{"$patch":{"removeIds":["u2"]}}"#.to_string(),
        ];
        std::fs::write(&path, lines.join("\n")).unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        let rows: Vec<(&str, &str)> = messages.iter().map(|m| (&*m.text, &*m.reply)).collect();
        assert_eq!(rows, vec![("alpaca", "ok alpaca"), ("bison", "")]);
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!(seen, rows + agent_rows + skips + errors);
    }

    /// Every session upstream's recorders wrote in tests/fixtures/gemini_flows reads as its
    /// `expected.json` transcript, with the tool events it pins and each record accounted for.
    #[test]
    fn every_recorded_flow_reads_its_expected_transcript() {
        let fixture =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../agrep-cli/tests/fixtures/gemini_flows");
        let expected: serde_json::Value =
            serde_json::from_slice(&std::fs::read(fixture.join("expected.json")).unwrap()).unwrap();
        let chats = fixture.join("home/.gemini/tmp/hash8888synthetic/chats");
        let mut files: Vec<PathBuf> = std::fs::read_dir(&chats)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect();
        files.sort();
        assert_eq!(files.len(), 196);
        let mut failures = Vec::new();
        for path in files {
            let (messages, events, tally) = parse(&path);
            let body = std::fs::read_to_string(&path).unwrap();
            let meta: serde_json::Value =
                serde_json::from_str(body.lines().next().unwrap()).unwrap();
            let case = &expected[meta["sessionId"].as_str().unwrap()];
            let rows: Vec<serde_json::Value> = messages
                .iter()
                .map(|m| serde_json::json!([&*m.who, &*m.text, &*m.reply]))
                .collect();
            if serde_json::Value::Array(rows.clone()) != case["rows"] {
                failures.push(format!("{}: {rows:?}", case["flow"]));
            }
            let mut calls: Vec<&str> = events.iter().map(|e| e.call_id.as_str()).collect();
            calls.sort_unstable();
            if case
                .get("events")
                .is_some_and(|pinned| *pinned != serde_json::json!(calls))
            {
                failures.push(format!("{}: events {calls:?}", case["flow"]));
            }
            let (seen, rows, agent_rows, skips, errors) = identity(&tally);
            assert_eq!(seen, rows + agent_rows + skips + errors, "{}", case["flow"]);
        }
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    /// Tool events follow the turns: an aborted turn's calls and a rewound one's go with them.
    #[test]
    fn undone_turns_take_their_tool_events_with_them() {
        let chats = Path::new(env!("CARGO_MANIFEST_DIR")).join(
            "../agrep-cli/tests/fixtures/gemini_flows/home/.gemini/tmp/hash8888synthetic/chats",
        );
        for (file, calls) in [
            (
                "session-2026-04-16T09-01-0f0f0f0f.jsonl",
                vec!["read-bison"],
            ),
            (
                "session-2026-05-16T09-01-1f1f1f1f.jsonl",
                vec!["read-bison"],
            ),
            ("session-2026-04-19T09-01-42424242.jsonl", vec!["read-b"]),
            ("session-2026-05-19T09-01-52525252.jsonl", vec!["read-b"]),
        ] {
            let (_, events, _) = parse(&chats.join(file));
            let got: Vec<&str> = events.iter().map(|e| e.call_id.as_str()).collect();
            assert_eq!(got, calls, "{file}");
        }
    }

    #[test]
    fn visible_text_strips_comments_like_upstream_in_one_pass() {
        for (raw, want) in [
            (" a<!--x-->b ", "ab"),
            ("<!-- a <!-- b --> c -->", "c -->"),
            ("<!<!--x-->--y-->z", "z"),
            ("<!---->k", "k"),
            ("<!--->k", "<!--->k"),
            ("<!-\u{200B}-x-->k\u{FEFF}", "k"),
            ("open <!-- never closed", "open <!-- never closed"),
        ] {
            assert_eq!(visible_text(raw), want, "{raw:?}");
        }
    }

    /// Hostile shapes stay linear: one id removed per re-sync from a long map, comment-dense
    /// prompts passing through a rewrite, one-in-one-out re-syncs behind a long map, whose gaps the
    /// rollback walk would cross again each time, many coalesced-looking copies ending like a
    /// removed turn with a huge part, which pairing would re-read per copy, many patches of a long
    /// reply that read the same, half with a call, which would compare or copy it each time,
    /// `/rewind` to each prompt from the last, whose answers a scan past undone rows would cross
    /// again each time, and prompts patched out of being rows and back before each `/rewind`,
    /// which a scan reading their content would cross again each time. Naively each takes minutes.
    #[test]
    fn hostile_re_syncs_parse_in_linear_time() {
        let root = temp_root("hostile");
        let path = root.join("session-2026-04-25T09-00-6c6c6c6c.jsonl");
        let messages = 20_000;
        let mut body = String::from(
            r#"{"sessionId":"6c6c6c6c-0000-4000-8000-000000000009","projectHash":"h"}"#,
        );
        body.push('\n');
        for n in 0..messages {
            body.push_str(&format!(
                r#"{{"id":"m{n}","type":"user","content":[{{"text":"p{n}"}}]}}"#
            ));
            body.push('\n');
        }
        let dense = "<!--x-->a".repeat(80_000);
        for id in ["c1", "c2", "c1copy", "c2copy"] {
            body.push_str(&format!(
                r#"{{"id":"{id}","type":"user","content":[{{"text":"{dense}"}}]}}"#
            ));
            body.push('\n');
        }
        body.push_str(r#"{"$patch":{"removeIds":["c1","c2"]}}"#);
        body.push('\n');
        for n in 0..messages {
            body.push_str(&format!(r#"{{"$patch":{{"removeIds":["m{n}"]}}}}"#));
            body.push('\n');
        }
        std::fs::write(&path, &body).unwrap();
        let started = std::time::Instant::now();
        let (rows, _, tally) = parse(&path);
        let elapsed = started.elapsed();
        assert_eq!(rows.len(), messages + 2);
        assert_eq!(tally.test_record_counts(Skip::Replay).3, 2);
        assert!(body.len() > 3_000_000);
        assert!(elapsed < std::time::Duration::from_secs(20), "{elapsed:?}");

        let path = root.join("session-2026-04-26T09-00-6d6d6d6d.jsonl");
        let (stable, rounds) = (400_000, 400_000);
        let mut body = String::from(
            r#"{"sessionId":"6d6d6d6d-0000-4000-8000-00000000000a","projectHash":"h"}"#,
        );
        body.push('\n');
        for n in 0..stable {
            body.push_str(&format!("{{\"id\":\"a{n}\"}}\n"));
        }
        for n in 0..rounds {
            body.push_str(&format!("{{\"id\":\"b{n}\"}}\n"));
            if n > 0 {
                let before = n - 1;
                body.push_str(&format!(
                    "{{\"$patch\":{{\"removeIds\":[\"b{before}\"]}}}}\n"
                ));
            }
        }
        std::fs::write(&path, &body).unwrap();
        let started = std::time::Instant::now();
        let (_, _, tally) = parse(&path);
        let elapsed = started.elapsed();
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!(seen, (stable + 2 * rounds) as u64);
        assert_eq!(seen, rows + agent_rows + skips + errors);
        assert!(body.len() > 25_000_000);
        assert!(elapsed < std::time::Duration::from_secs(10), "{elapsed:?}");

        let path = root.join("session-2026-04-27T09-00-6e6e6e6e.jsonl");
        let copies = 150_000;
        let mut body = String::from(
            r#"{"sessionId":"6e6e6e6e-0000-4000-8000-00000000000c","projectHash":"h"}"#,
        );
        body.push('\n');
        let huge = "x".repeat(8 << 20);
        body.push_str(&format!(
            r#"{{"id":"r","type":"user","content":[{{"text":"{huge}"}},{{"text":"a"}}]}}"#
        ));
        body.push('\n');
        for n in 0..copies {
            body.push_str(&format!(
                "{{\"id\":\"c{n}\",\"type\":\"user\",\"content\":[{{\"text\":\"b\"}},{{\"text\":\"a\"}}]}}\n"
            ));
        }
        body.push_str(r#"{"$patch":{"removeIds":["r"]}}"#);
        std::fs::write(&path, &body).unwrap();
        let started = std::time::Instant::now();
        let (rows, _, _) = parse(&path);
        let elapsed = started.elapsed();
        assert_eq!(rows.len(), copies + 1);
        assert!(elapsed < std::time::Duration::from_secs(8), "{elapsed:?}");

        let path = root.join("session-2026-04-28T09-00-6f6f6f6f.jsonl");
        let patches = 200_000;
        let mut body = String::from(
            r#"{"sessionId":"6f6f6f6f-0000-4000-8000-00000000000e","projectHash":"h"}"#,
        );
        body.push('\n');
        body.push_str(r#"{"id":"u","type":"user","content":"read"}"#);
        body.push('\n');
        let comment = format!("ok<!--{}-->", "x".repeat(4 << 20));
        body.push_str(&format!(
            r#"{{"id":"g","type":"gemini","content":"{comment}"}}"#
        ));
        body.push('\n');
        for n in 0..patches {
            let call = if n % 2 == 0 {
                r#",{"functionCall":{"id":"c","name":"read_file"}}"#
            } else {
                ""
            };
            body.push_str(&format!(
                "{{\"$patch\":{{\"updates\":[{{\"id\":\"g\",\"content\":[{{\"text\":\"ok\"}}{call}]}}]}}}}\n"
            ));
        }
        std::fs::write(&path, &body).unwrap();
        let started = std::time::Instant::now();
        let (rows, _, _) = parse(&path);
        let elapsed = started.elapsed();
        assert_eq!(rows.len(), 1);
        assert!(
            rows[0].reply.starts_with("ok<!--xxx"),
            "the recorded reply is kept"
        );
        assert!(elapsed < std::time::Duration::from_secs(8), "{elapsed:?}");

        let path = root.join("session-2026-04-29T09-00-70707070.jsonl");
        let prompts = 50_000;
        let mut body = String::from(
            r#"{"sessionId":"70707070-0000-4000-8000-00000000000f","projectHash":"h"}"#,
        );
        body.push('\n');
        let mut answers = Vec::new();
        for n in 0..prompts {
            body.push_str(&format!(
                "{{\"id\":\"u{n}\",\"type\":\"user\",\"content\":\"p{n}\"}}\n\
                 {{\"id\":\"g{n}\",\"type\":\"gemini\",\"content\":\"r{n}\",\
                 \"toolCalls\":[{{\"id\":\"c{n}\",\"name\":\"read_file\"}}]}}\n"
            ));
            answers.push(format!("\"g{n}\""));
        }
        body.push_str(&format!(
            "{{\"$patch\":{{\"removeIds\":[{}]}}}}\n",
            answers.join(",")
        ));
        for n in (0..prompts).rev() {
            body.push_str(&format!("{{\"$rewindTo\":\"u{n}\"}}\n"));
        }
        std::fs::write(&path, &body).unwrap();
        let started = std::time::Instant::now();
        let (rows, events, _) = parse(&path);
        let elapsed = started.elapsed();
        assert!(
            rows.is_empty() && events.is_empty(),
            "{} {}",
            rows.len(),
            events.len()
        );
        assert!(elapsed < std::time::Duration::from_secs(8), "{elapsed:?}");

        let path = root.join("session-2026-04-30T09-00-71717171.jsonl");
        let (prompts, turns) = (20_000, 20_000);
        let mut body = String::from(
            r#"{"sessionId":"71717171-0000-4000-8000-000000000010","projectHash":"h"}"#,
        );
        body.push('\n');
        for n in 0..prompts {
            body.push_str(&format!(
                "{{\"id\":\"u{n}\",\"type\":\"user\",\"content\":\"p{n}\"}}\n"
            ));
        }
        for n in 0..turns {
            body.push_str(&format!(
                "{{\"id\":\"g{n}\",\"type\":\"gemini\",\"content\":\"\"}}\n"
            ));
        }
        let emptied: Vec<String> = (1..prompts)
            .map(|n| format!("{{\"id\":\"u{n}\",\"content\":\"\"}}"))
            .collect();
        body.push_str(&format!(
            "{{\"$patch\":{{\"updates\":[{}]}}}}\n",
            emptied.join(",")
        ));
        for n in 1..prompts {
            body.push_str(&format!(
                "{{\"$patch\":{{\"updates\":[{{\"id\":\"u{n}\",\"content\":\"p{n}\"}}],\
                 \"orderIds\":[\"u{n}\"]}}}}\n{{\"$rewindTo\":\"u{n}\"}}\n"
            ));
        }
        std::fs::write(&path, &body).unwrap();
        let started = std::time::Instant::now();
        let (rows, _, _) = parse(&path);
        let elapsed = started.elapsed();
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(rows.len(), 1);
        assert!(elapsed < std::time::Duration::from_secs(8), "{elapsed:?}");
    }

    /// A read is proven empty (`ReadOutcome::Undone`) only when the file undid what it held in
    /// records the read saw whole: never on a torn last line, a read ending mid-record, a file
    /// that had nothing to undo, or one that still reads as rows.
    #[test]
    fn only_a_whole_read_of_an_undone_session_is_proven_empty() {
        let root = temp_root("undone");
        let path = root.join("session-2026-05-01T09-00-72727272.jsonl");
        let meta = r#"{"sessionId":"72727272-0000-4000-8000-000000000011","projectHash":"h"}"#;
        let prompt = r#"{"id":"u1","type":"user","content":"alpaca"}"#;
        let reply = r#"{"id":"g1","type":"gemini","content":"ok alpaca"}"#;
        let rewind = r#"{"$rewindTo":"u1"}"#;
        let hook = r#"{"id":"h1","type":"user","content":"<hook_context>x</hook_context>"}"#;
        for (lines, end, want) in [
            (vec![meta, prompt, reply, rewind], "\n", ReadOutcome::Undone),
            (vec![meta, prompt, reply, rewind], "", ReadOutcome::Complete),
            (
                vec![meta, prompt, reply, rewind, r#"{"id":"u2","ty"#],
                "",
                ReadOutcome::Complete,
            ),
            (vec![meta, hook], "\n", ReadOutcome::Complete),
            (vec![meta, prompt, reply], "\n", ReadOutcome::Complete),
            (vec![prompt, reply, rewind], "\n", ReadOutcome::Complete),
        ] {
            std::fs::write(&path, format!("{}{end}", lines.join("\n"))).unwrap();
            let (_, _, outcome) = parse_with_tally(&path, &Tally::default());
            assert_eq!(outcome, want, "{lines:?} {end:?}");
        }
        let _ = std::fs::remove_dir_all(&root);
    }

    /// A declined tool sets the history back to before its prompt, after the UI records
    /// 'Request cancelled.'. Coalesced, the first prompt stays in the environment's copy, which
    /// `/rewind`'s conversion then leaves out with a `$rewindTo` of its own; with nothing before
    /// the prompt the decline itself rolls everything back. Neither is the person undoing it. A
    /// person's `/rewind`, also one ending on a declined turn the CLI never rolled back, still is.
    #[test]
    fn declined_prompts_survive_the_rollbacks_around_them() {
        let root = temp_root("declined");
        let env = r#"{"text":"<session_context>\nx\n</session_context>"}"#;
        let declined =
            r#""toolCalls":[{"id":"t1","name":"run_shell_command","status":"cancelled"}]"#;
        let notice =
            |id: &str| format!(r#"{{"id":"{id}","type":"info","content":"Request cancelled."}}"#);
        let flows = [
            (
                vec![
                    format!(r#"{{"id":"env","type":"user","content":[{env}]}}"#),
                    r#"{"$set":{"lastUpdated":"2026-04-28T09:00:00.000Z"}}"#.to_string(),
                    r#"{"id":"u1","type":"user","content":[{"text":"alpaca"}]}"#.to_string(),
                    r#"{"$set":{"lastUpdated":"2026-04-28T09:01:00.000Z"}}"#.to_string(),
                    format!(r#"{{"id":"g1","type":"gemini","content":"",{declined}}}"#),
                    notice("n1"),
                    format!(r#"{{"id":"c1","type":"user","content":[{env},{{"text":"alpaca"}}]}}"#),
                    r#"{"$set":{"lastUpdated":"2026-04-28T09:02:00.000Z"}}"#.to_string(),
                    r#"{"$patch":{"removeIds":["env","u1","g1","n1"]}}"#.to_string(),
                    r#"{"id":"u2","type":"user","content":[{"text":"bison"}]}"#.to_string(),
                    r#"{"$set":{"lastUpdated":"2026-04-28T09:03:00.000Z"}}"#.to_string(),
                    r#"{"$rewindTo":"u2"}"#.to_string(),
                    r#"{"$rewindTo":"c1"}"#.to_string(),
                    r#"{"id":"u3","type":"user","content":[{"text":"cheetah"}]}"#.to_string(),
                ],
                vec!["alpaca", "cheetah"],
            ),
            (
                vec![
                    r#"{"id":"n0","type":"info","content":"No conversation found to save."}"#
                        .to_string(),
                    r#"{"id":"u1","type":"user","content":[{"text":"alpaca"}]}"#.to_string(),
                    r#"{"$set":{"lastUpdated":"2026-04-28T09:01:00.000Z"}}"#.to_string(),
                    format!(r#"{{"id":"g1","type":"gemini","content":"",{declined}}}"#),
                    notice("n1"),
                    r#"{"$rewindTo":"n0"}"#.to_string(),
                    r#"{"id":"u2","type":"user","content":[{"text":"bison"}]}"#.to_string(),
                ],
                vec!["alpaca", "bison"],
            ),
            (
                vec![
                    r#"{"id":"u1","type":"user","content":[{"text":"alpaca"}]}"#.to_string(),
                    r#"{"id":"u2","type":"user","content":[{"text":"bison"}]}"#.to_string(),
                    r#"{"id":"u3","type":"user","content":[{"text":"cheetah"}]}"#.to_string(),
                    r#"{"$set":{"lastUpdated":"2026-04-28T09:03:00.000Z"}}"#.to_string(),
                    r#"{"$rewindTo":"u3"}"#.to_string(),
                    r#"{"$rewindTo":"u2"}"#.to_string(),
                ],
                vec!["alpaca"],
            ),
            (
                vec![
                    r#"{"id":"u1","type":"user","content":[{"text":"alpaca"}]}"#.to_string(),
                    r#"{"id":"a1","type":"gemini","content":"ok alpaca"}"#.to_string(),
                    r#"{"id":"u2","type":"user","content":[{"text":"bison"}]}"#.to_string(),
                    format!(r#"{{"id":"g2","type":"gemini","content":"",{declined}}}"#),
                    notice("n2"),
                    r#"{"$rewindTo":"u2"}"#.to_string(),
                    r#"{"id":"u3","type":"user","content":[{"text":"cheetah"}]}"#.to_string(),
                ],
                vec!["alpaca", "cheetah"],
            ),
            (
                vec![
                    r#"{"id":"u1","type":"user","content":[{"text":"alpaca"}]}"#.to_string(),
                    r#"{"id":"a1","type":"gemini","content":"ok alpaca"}"#.to_string(),
                    r#"{"id":"u2","type":"user","content":[{"text":"bison"}]}"#.to_string(),
                    format!(r#"{{"id":"g2","type":"gemini","content":"",{declined}}}"#),
                    notice("n2"),
                    r#"{"id":"u3","type":"user","content":[{"text":"cheetah"}]}"#.to_string(),
                    notice("n3"),
                    r#"{"$set":{"lastUpdated":"2026-04-28T09:05:00.000Z"}}"#.to_string(),
                    r#"{"$patch":{"removeIds":["n2","u3","n3"]}}"#.to_string(),
                    r#"{"$rewindTo":"u2"}"#.to_string(),
                    r#"{"id":"u4","type":"user","content":[{"text":"dingo"}]}"#.to_string(),
                ],
                vec!["alpaca", "dingo"],
            ),
        ];
        for (n, (lines, want)) in flows.into_iter().enumerate() {
            let path = root.join(format!("session-2026-04-28T09-00-6f6f6f6{n}.jsonl"));
            let head = r#"{"sessionId":"6f6f6f6f-0000-4000-8000-00000000000d","projectHash":"h"}"#;
            std::fs::write(&path, format!("{head}\n{}", lines.join("\n"))).unwrap();
            let (messages, _, tally) = parse(&path);
            let texts: Vec<&str> = messages.iter().map(|m| &*m.text).collect();
            assert_eq!(texts, want, "flow {n}");
            let (seen, rows, agent_rows, skips, errors) = identity(&tally);
            assert_eq!(seen, rows + agent_rows + skips + errors, "flow {n}");
        }
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn masking_keeps_the_recorded_tool_output_and_the_turns_own_moment() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../agrep-cli/tests/fixtures/gemini_flows/home/.gemini/tmp/hash8888synthetic")
            .join("chats/session-2026-04-04T09-01-03030303.jsonl");
        let (messages, events, _) = parse(&path);
        let outputs: Vec<&str> = events.iter().map(|e| e.output.as_str()).collect();
        assert_eq!(
            outputs,
            vec!["export const alpaca = 1;", "export const bison = 1;"]
        );
        let at = |iso| crate::ingest::parse_timestamp::rfc3339(Some(iso));
        let stamps: Vec<i64> = messages.iter().map(|m| m.ts).collect();
        assert_eq!(
            stamps,
            vec![
                at("2026-04-04T09:02:00.000Z"),
                at("2026-04-04T09:06:00.000Z"),
                at("2026-04-04T09:10:00.000Z")
            ]
        );
    }

    #[test]
    fn rewinding_past_copies_whose_originals_follow_the_target_never_panics() {
        // the stable-id environment turn stays first in the map through compression
        let root = temp_root("rewind-env");
        let path = root.join("session-2026-04-24T09-00-6d6d6d6d.jsonl");
        let lines = [
            r#"{"sessionId":"6d6d6d6d-0000-4000-8000-000000000008","projectHash":"h"}"#,
            r#"{"id":"env","timestamp":"2026-04-24T09:00:00.000Z","type":"user","content":[{"text":"<session_context>x</session_context>"}]}"#,
            r#"{"id":"a","timestamp":"2026-04-24T09:00:01.000Z","type":"user","content":[{"text":"alpha"}]}"#,
            r#"{"id":"b","timestamp":"2026-04-24T09:01:01.000Z","type":"user","content":[{"text":"beta"}]}"#,
            r#"{"$set":{"sessionId":"6d6d6d6d-0000-4000-8000-000000000008"}}"#,
            r#"{"id":"s","timestamp":"2026-04-24T09:02:00.000Z","type":"user","content":[{"text":"<state_snapshot>a and b</state_snapshot>"}]}"#,
            r#"{"id":"b2","timestamp":"2026-04-24T09:02:00.000Z","type":"user","content":[{"text":"beta"}]}"#,
            r#"{"$patch":{"removeIds":["a","b"]}}"#,
            r#"{"$rewindTo":"env"}"#,
            r#"{"$rewindTo":"never-recorded"}"#,
        ];
        std::fs::write(&path, lines.join("\n")).unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(turns(&messages), vec![(0, "user", "alpha")]);
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!(seen, rows + agent_rows + skips + errors);
        assert_eq!(tally.test_record_counts(Skip::Unreferenced).3, 4);
    }

    #[test]
    fn a_jsonl_sibling_supersedes_its_legacy_json() {
        let root = temp_root("supersede");
        let chats = root.join("hash/chats");
        std::fs::create_dir_all(chats.join("parent-session")).unwrap();
        std::fs::create_dir_all(root.join("hash/logs")).unwrap();
        let migrated = chats.join("session-2026-03-10T08-00-6d6d6d6d.json");
        let current = chats.join("session-2026-03-10T08-00-6d6d6d6d.jsonl");
        let legacy = chats.join("session-2026-03-11T08-00-6e6e6e6e.json");
        for path in [&migrated, &current, &legacy] {
            std::fs::write(path, "{}").unwrap();
        }
        std::fs::write(chats.join("parent-session/6f6f6f6f.jsonl"), "{}").unwrap();
        let log = root.join("hash/logs/session-6d6d6d6d.jsonl");
        std::fs::write(&log, "{}").unwrap();

        let (mut files, retired) = session_files(&root);
        files.sort();
        assert_eq!(files, vec![current.clone(), legacy.clone()]);
        assert_eq!(
            retired.into_iter().collect::<Vec<_>>(),
            vec![migrated.clone()]
        );
        assert!(Gemini.store_content(&current));
        assert!(Gemini.store_content(&legacy));
        assert!(!Gemini.store_content(&migrated));
        assert!(!Gemini.store_content(&log));

        std::fs::remove_file(&current).unwrap();
        assert!(Gemini.store_content(&migrated));
        let (mut files, retired) = session_files(&root);
        files.sort();
        assert_eq!(files, vec![migrated, legacy]);
        assert!(retired.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[cfg(unix)]
    #[test]
    fn discovery_rejects_symlinked_projects_chats_and_sessions() {
        use std::os::unix::fs::symlink;
        let root = temp_root("links");
        let outside = root.with_extension("outside");
        std::fs::create_dir_all(root.join("safe/chats")).unwrap();
        std::fs::create_dir_all(outside.join("chats")).unwrap();
        std::fs::write(root.join("safe/chats/session-safe.json"), "{}").unwrap();
        let outside_file = outside.join("chats/session-outside.json");
        std::fs::write(&outside_file, "{}").unwrap();
        symlink(&outside, root.join("linked-project")).unwrap();
        std::fs::create_dir_all(root.join("linked-chats-project")).unwrap();
        symlink(
            outside.join("chats"),
            root.join("linked-chats-project/chats"),
        )
        .unwrap();
        symlink(&outside_file, root.join("safe/chats/session-linked.json")).unwrap();
        // a symlinked .jsonl must not retire the real .json it would shadow
        symlink(&outside_file, root.join("safe/chats/session-safe.jsonl")).unwrap();

        let (files, retired) = session_files(&root);
        assert_eq!(files, vec![root.join("safe/chats/session-safe.json")]);
        assert!(retired.is_empty());
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(outside);
    }
}
