//! Serde shapes of rollout records and the visible text of their content blocks.

use std::borrow::Cow;

use serde::Deserialize;
use serde_json::value::RawValue;

// Borrowed deserialization (soundness argument in claude.rs); RawValue defers the DOMs
// until a call pairs up, and the byte prefilter keeps reasoning blobs out of serde.
#[derive(Deserialize)]
pub(super) struct Line<'a> {
    #[serde(rename = "type", borrow)]
    pub(super) ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) timestamp: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) payload: Option<Payload<'a>>,
}

#[derive(Deserialize)]
pub(super) struct Payload<'a> {
    #[serde(rename = "type", borrow)]
    pub(super) ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) role: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) content: Option<Vec<Block<'a>>>,
    // session_meta fields
    #[serde(borrow)]
    pub(super) id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) cwd: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) thread_source: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) parent_thread_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) agent_path: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) source: Option<&'a RawValue>,
    // Inter-agent handoffs are response_item/agent_message records. Their task body
    // may be encrypted, but the routing metadata and visible header remain available.
    #[serde(borrow)]
    pub(super) author: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) recipient: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) turn_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) internal_chat_message_metadata_passthrough: Option<ChatMessageMeta<'a>>,
    // turn_context carries the active model (e.g. "gpt-5.3-codex-spark").
    #[serde(borrow)]
    pub(super) model: Option<Cow<'a, str>>,
    /// Tool-call fields (`name` through `action`): function_call {name,
    /// arguments(JSON-string), call_id}; function_call_output {call_id, output};
    /// custom_tool_call {name, input, status}; web_search_call {action:{query,...}};
    /// tool_search_call carries `arguments` as an OBJECT, so it stays raw and each
    /// consumer decodes its own shape.
    #[serde(borrow)]
    pub(super) name: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) arguments: Option<&'a RawValue>,
    #[serde(borrow)]
    pub(super) call_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) output: Option<&'a RawValue>,
    #[serde(borrow)]
    pub(super) input: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) status: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) action: Option<&'a RawValue>,
    // item_completed events: the thread the item belongs to, and the item.
    #[serde(borrow)]
    pub(super) thread_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) item: Option<Item<'a>>,
}

/// An `item_completed` event's item the parser reads: Codex opening its review mode
/// (`EnteredReviewMode`, with the hint Codex shows for it) or a message a thread received
/// (`UserMessage`).
#[derive(Deserialize)]
pub(super) struct Item<'a> {
    #[serde(rename = "type", borrow)]
    pub(super) ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) content: Option<Vec<Block<'a>>>,
    #[serde(borrow)]
    pub(super) user_facing_hint: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
pub(super) struct ChatMessageMeta<'a> {
    #[serde(borrow)]
    pub(super) turn_id: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
pub(super) struct Block<'a> {
    #[serde(rename = "type", borrow)]
    pub(super) ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) text: Option<Cow<'a, str>>,
}

/// Concatenate visible human/routed text blocks. Routed `agent_message` handoffs have
/// appeared as both `input_text` and `output_text`; dropping the latter prevents the child
/// boundary from ever opening and suppresses the entire forked transcript.
pub(super) fn extract_text(blocks: &[Block<'_>]) -> Option<String> {
    join_blocks(blocks, &["input_text", "output_text", "text"])
}

/// Concatenate the assistant's visible prose blocks (`output_text`, also accept `text`).
pub(super) fn extract_assistant(blocks: &[Block<'_>]) -> Option<String> {
    join_blocks(blocks, &["output_text", "text"])
}

/// Newline-join the text of blocks whose type is one of `kinds`; `None` when only
/// whitespace remains.
fn join_blocks(blocks: &[Block<'_>], kinds: &[&str]) -> Option<String> {
    let mut out = String::new();
    for block in blocks {
        if !block.ty.as_deref().is_some_and(|ty| kinds.contains(&ty)) {
            continue;
        }
        if let Some(text) = block.text.as_deref() {
            if !out.is_empty() {
                out.push('\n');
            }
            out.push_str(text);
        }
    }
    if out.trim().is_empty() {
        None
    } else {
        Some(out)
    }
}
