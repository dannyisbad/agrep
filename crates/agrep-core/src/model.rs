//! The normalized units every adapter emits: [`Message`] (a human turn + reply) and
//! [`Event`] (a tool call / subagent step in the same session).

use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct Message {
    /// Registered source-adapter name.
    pub agent: &'static str,
    /// Project bucket (basename of the working dir, disambiguated for generic names).
    /// `Arc<str>` (here and below): messages round-trip through the parse cache and the
    /// dedupe set every run, so field copies must be refcount bumps, not string clones.
    pub project: Arc<str>,
    /// Session/chat id this message belongs to.
    pub session: Arc<str>,
    /// Epoch milliseconds; 0 if unknown.
    pub ts: i64,
    /// 0-based index of this message within its session (ingest order).
    pub turn: u32,
    /// The human-authored text (the user's words). Wrappers/tool-results already stripped.
    pub text: Arc<str>,
    /// User-side row type: "user" for real prompts, "subagent" for side-chat turns
    /// (a Task/fork child conversation - real work, marked), "control" for non-model
    /// control markers, "synthetic" for test traffic, "recap" for continuation
    /// summaries, or "harness" for generated benchmark/controller prompts.
    pub who: Arc<str>,
    /// Model that produced the agent's side of this turn ("claude-opus-4-8",
    /// "gpt-5.3-codex-spark", "gemini-3.1-pro-preview"). Empty when the source omits it.
    pub model: Arc<str>,
    /// How `model` was attributed: "explicit", "session", "temporal_session",
    /// "unknown", "control", "synthetic", "recap", "harness", "explicit_harness",
    /// or "ambiguous_session".
    pub model_source: Arc<str>,
    /// The agent's reply to this turn, trimmed for display: its text blocks joined with one
    /// space and cut at `ingest::REPLY_CAP` characters (`…` appended when a block was cut), the
    /// exact text every release has hashed for reply handles. Empty if none was captured.
    pub reply: Arc<str>,
    /// Original normalized reply length before the ingest safety cap.
    pub reply_chars: usize,
    /// Where `reply`'s blocks join and, past the cap, how it ended; see [`ReplyShape`].
    pub reply_shape: Arc<ReplyShape>,
    /// True when this turn lives in a SIDE session (subagent/Task/fork child). The
    /// normalizer turns it into `who="subagent"`; adapters set it when the store
    /// proves provenance.
    pub side: bool,
    /// The parent session id for side sessions; empty for roots. Surfaces in
    /// sessions.jsonl so consumers can link child chats to their parent.
    pub parent: Arc<str>,
}

/// The mutable form the adapters build while parsing one file (replies stream in as
/// appends, models backfill). `freeze` converts to the shared form exactly once, when
/// the parse is done - so the Arc fields never need in-place mutation.
pub struct RawMessage {
    pub agent: &'static str,
    pub project: String,
    pub session: String,
    pub ts: i64,
    pub turn: u32,
    pub text: String,
    pub model: String,
    pub reply: String,
    pub reply_chars: usize,
    pub reply_shape: ReplyShape,
    pub side: bool,
    pub parent: String,
}

impl RawMessage {
    pub fn freeze(self) -> Message {
        let reply_chars = if self.reply_chars == 0 {
            self.reply.chars().count()
        } else {
            self.reply_chars
        };
        Message {
            agent: self.agent,
            project: self.project.into(),
            session: self.session.into(),
            ts: self.ts,
            turn: self.turn,
            text: self.text.into(),
            who: "user".into(),
            model_source: if self.model.is_empty() {
                "unknown".into()
            } else {
                "explicit".into()
            },
            model: self.model.into(),
            reply: self.reply.into(),
            reply_chars,
            reply_shape: Arc::new(self.reply_shape),
            side: self.side,
            parent: self.parent.into(),
        }
    }
}

/// The block structure of a reply, kept beside its text so the text stays the one 0.4.2
/// published. Offsets count Unicode scalar values (Rust `char`s, Python `str` indices).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ReplyShape {
    /// Characters held in `reply` (its cap marker included once cut).
    pub head_chars: u32,
    /// Offset in `reply` of each space that joins a block to the one before it; the later block
    /// starts one past it. Empty for a single block.
    pub breaks: Vec<u32>,
    /// Per join (each break, then a tail block past the cap that made no break): 1 when the
    /// agent made a work tool call between the two blocks, 0 when none, 2 when the adapter
    /// does not record block/tool order (`WORKED_*`).
    pub worked: Vec<u8>,
    /// How many joins the reply made; `worked` fills up to it.
    pub joins: u32,
    /// The join before the first block: work between the row's start (its prompt, or for a
    /// compaction recap the previous row's last block) and that block (`WORKED_*`).
    pub lead: u8,
    /// Past the cap, the latest block's last `ingest::REPLY_TAIL_CAP` characters; empty while
    /// the reply is whole.
    pub end: String,
    /// `end` holds only the tail of that block (it ran past `REPLY_TAIL_CAP`).
    pub end_cut: bool,
}

/// A shape no adapter has described yet (a row restored from an older cache): its lead join is
/// unknown, not "no work".
impl Default for ReplyShape {
    fn default() -> Self {
        Self {
            head_chars: 0,
            breaks: Vec::new(),
            worked: Vec::new(),
            joins: 0,
            lead: WORKED_UNKNOWN,
            end: String::new(),
            end_cut: false,
        }
    }
}

/// `ReplyShape::worked` values.
pub const WORKED_NO: u8 = 0;
pub const WORKED_YES: u8 = 1;
pub const WORKED_UNKNOWN: u8 = 2;

/// One tool call or subagent step inside a session. Written to per-session files under
/// `data/events/`, separate from the canonical message stream. Inputs/outputs are capped
/// summaries; original lengths disclose loss.
#[derive(Debug, Clone)]
pub struct Event {
    /// Source agent: "claude", "codex", "opencode", "antigravity".
    pub agent: &'static str,
    /// Session this event belongs to (the PARENT session for subagent events).
    pub session: String,
    /// Epoch milliseconds; 0 if unknown.
    pub ts: i64,
    /// "tool" | "control" | "subagent_start" | "subagent_result".
    pub kind: &'static str,
    /// Tool name ("Bash", "shell", "run_command") or subagent title/sender.
    pub name: String,
    /// Compact human-meaningful input summary (command line, file path, prompt), capped.
    pub input: String,
    /// Tool output / subagent result, capped.
    pub output: String,
    /// Original character lengths before the summaries were capped.
    pub input_chars: usize,
    pub output_chars: usize,
    /// Original UTF-8 byte length of the output before it was capped. This cannot be
    /// reconstructed from a capped Unicode excerpt.
    pub output_bytes: usize,
    /// Success if the store recorded one (is_error / status / exit hints); None when unknown.
    pub ok: Option<bool>,
    /// The store's own correlation id (tool_use id / call_id / callID). Synthesized
    /// (unique within the session) when the store has none. Dedupe/provenance key.
    pub call_id: String,
    /// Subagent events: the child's own session id when it is independently viewable.
    pub child_session: String,
    /// Facts the store recorded for this call beside its display text, as space-separated
    /// `key=value` tokens (Claude: an agent call's `name`/`status`, a message's `to`/`handback`,
    /// and when either `returned`; any adapter: `after_reply=1` for a call no reply text of
    /// its turn followed). Never capped; empty when the store recorded none.
    pub meta: String,
    /// The task-list facts a todo/plan/task call carried, as compact JSON kept whole past the
    /// display cap (`ingest::task_state`, `ingest::task_listing`); empty for every other call.
    pub state: String,
}
