//! Reading a recorded message: its PartListUnion content as text, the kind of turn it is in
//! upstream's history, and the canned and injected text that is nobody's words.

use serde_json::Value;

use crate::ingest::is_wrapper;

/// Compression (upstream `ChatCompressionService`) records the model-written `<state_snapshot>`
/// as a user turn, then this canned model acknowledgement, before the kept tail.
pub(super) const COMPRESSION_ACK: &str = "Got it. Thanks for the additional context!";

/// Upstream's interrupted-turn closer (`GeminiChat.closeUnansweredToolResponseTurn`): a model turn
/// the CLI appends after a cancelled tool call, never something the model said.
pub(super) const INTERRUPTED_PLACEHOLDER: &str =
    "[The previous response was interrupted before it completed.]";

/// The text a PartListUnion carries: the string itself, or its text parts joined the way
/// upstream's `partToString` joins them. Thought parts are reasoning, never prose.
pub(super) fn part_text(value: &Value) -> String {
    parts_text(parts_of(value))
}

pub(super) fn parts_text(parts: &[Value]) -> String {
    let mut out = String::new();
    for text in parts.iter().filter_map(prose) {
        out.push_str(text);
    }
    out
}

/// A part's text when it is prose: a string, or a text part that is no thought.
pub(super) fn prose(part: &Value) -> Option<&str> {
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

/// A PartListUnion as its parts: a lone string or part is a list of one.
pub(super) fn parts_of(value: &Value) -> &[Value] {
    match value {
        Value::Array(parts) => parts,
        part => std::slice::from_ref(part),
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

/// New `content` for a message in the map, as a `$patch` or a record under the same id writes
/// it. Upstream records a model turn's reply cleaned since 0.53 (`responseText`, geminiChat.ts
/// 1547-1659 at fb972b2) and as written before, and a re-sync writes back the history's raw
/// parts (chatRecordingService.ts 1364-1381): when those read the same (see `visible_text`), the
/// turn keeps the reply it recorded, among the parts' calls (see `with_recorded_reply`), and
/// `copied` stays false. A turn without one takes the content: after Esc its calls have a record
/// of their own (1133-1157), which a copy of the turn then pairs with. `reply` caches the
/// visible text: patches that write the same back can come by the hundred thousand.
pub(super) fn absorb(
    message: &mut serde_json::Map<String, Value>,
    reply: &mut Option<String>,
    copied: &mut bool,
    content: &Value,
) {
    let gemini = message.get("type").and_then(Value::as_str) == Some("gemini");
    let visible = |content: &Value| visible_text(&part_text(content));
    let kept = gemini && {
        let new = visible(content);
        let old =
            reply.get_or_insert_with(|| message.get("content").map(visible).unwrap_or_default());
        let same = *old == new;
        *old = new;
        same
    };
    if !kept {
        message.insert("content".to_string(), content.clone());
        *copied = gemini && content.is_array();
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

/// Text as upstream records a reply (`responseText`): zero-width characters removed, then HTML
/// comments until none is left, then trimmed. One pass: a comment closes at the first `-->` after
/// the leftmost open `<!--`, and a removal can join the text around it into a new `<!--`.
pub(super) fn visible_text(text: &str) -> String {
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

/// A user turn carrying a functionResponse part is a tool result, not something typed.
pub(super) fn carries_function_response(value: &Value) -> bool {
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
pub(super) fn is_injected_context(text: &str) -> bool {
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
pub(super) fn past_injected(mut parts: &[Value]) -> &[Value] {
    while let Some((first, rest)) = parts.split_first() {
        if rest.is_empty() || !is_injected_context(&parts_text(std::slice::from_ref(first))) {
            break;
        }
        parts = rest;
    }
    parts
}

/// What a user turn's row reads: the typed `displayContent` when it has text, else the content
/// past its leading injected context.
pub(super) fn prompt_text(message: &Value, content: &Value) -> String {
    let typed = message
        .get("displayContent")
        .map(part_text)
        .filter(|text| !text.trim().is_empty());
    typed.unwrap_or_else(|| parts_text(past_injected(parts_of(content))))
}

/// A prompt or a snapshot, as `emit_messages` reads a row's message: the model turns after it, up
/// to the next one, answer it. A copy, which emits no row, is no such message once paired.
pub(super) fn opens_exchange(message: &Value) -> bool {
    let content = message.get("content").unwrap_or(&Value::Null);
    if message.get("type").and_then(Value::as_str) != Some("user")
        || carries_function_response(content)
    {
        return false;
    }
    let text = prompt_text(message, content);
    !text.trim().is_empty() && !is_wrapper(&text) && !is_injected_context(&text)
}

pub(super) fn is_state_snapshot(text: &str) -> bool {
    text.contains("<state_snapshot>") && text.contains("</state_snapshot>")
}

/// An info, warning or error item of the CLI's own: `useHistory().addItem` records every one as a
/// session message with its text as content (useHistoryManager.ts 91-124 at fb972b2), outside the
/// model's history, so the next re-sync removes it. Auto-compression's can stay for good: the UI
/// records it through the recorder it bound before `tryCompressChat` swapped in a new one
/// (AppContainer.tsx 235-237, client.ts 1236-1250), and the new one never learns its id.
pub(super) fn is_notice(message: &Value) -> bool {
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
pub(super) fn history_kind(message: &Value) -> &str {
    if is_binary_turn(message) {
        return "user";
    }
    message.get("type").and_then(Value::as_str).unwrap_or("")
}

/// Whether a message's content is the history's parts rather than the reply upstream's own
/// recorder saved: a re-sync records a model turn's raw stream as an array of parts, which the
/// UI would have shown cleaned (`responseText`, since gemini-cli 0.53), while a recorded reply
/// is a string saved exactly as shown, cleaned or not by the version that wrote it.
pub(super) fn is_history_copy(message: &Value) -> bool {
    message.get("type").and_then(Value::as_str) == Some("gemini")
        && message.get("content").is_some_and(Value::is_array)
}

/// A message upstream's `convertSessionToClientHistory` (sessionUtils.ts 110-228 at fb972b2) leaves
/// out of the history it rebuilds on `/rewind` and `--resume`: info, error and warning messages,
/// binary turns too, and user turns `isIgnoredUserContent` (97-105) rejects. That tests the
/// trimmed `partListUnionToString`: `partToString` verbose (partUtils.ts 18-82), parts joined.
pub(super) fn conversion_skips(message: &Value) -> bool {
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

/// JS `typeof value === 'object' && value !== null`, the test upstream applies to `$set`/`$patch`.
pub(super) fn is_js_object(value: &Value) -> bool {
    matches!(value, Value::Object(_) | Value::Array(_))
}

pub(super) fn js_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|n| n != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(_) | Value::Object(_) => true,
    }
}
