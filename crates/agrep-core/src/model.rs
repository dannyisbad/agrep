//! The normalized units every adapter emits: [`Message`] (a human turn + reply) and
//! [`Event`] (a tool call / subagent step in the same session).

#[derive(Debug, Clone)]
pub struct Message {
    /// Source agent: "claude", "codex", "opencode", "antigravity", "gemini".
    pub agent: &'static str,
    /// Project bucket (basename of the working dir, disambiguated for generic names).
    pub project: String,
    /// Session/chat id this message belongs to.
    pub session: String,
    /// Epoch milliseconds; 0 if unknown.
    pub ts: i64,
    /// 0-based index of this message within its session (ingest order).
    pub turn: u32,
    /// The human-authored text (the user's words). Wrappers/tool-results already stripped.
    pub text: String,
    /// Model that produced the agent's side of this turn ("claude-opus-4-8",
    /// "gpt-5.3-codex-spark", "gemini-3.1-pro-preview"). Empty when the source omits it.
    pub model: String,
    /// The agent's reply to this turn, trimmed for display. Empty if none was captured.
    pub reply: String,
}

/// One tool call or subagent step inside a session. Written to per-session files under
/// `data/events/` — a parallel stream to `messages.jsonl` that the existing embed/affect
/// pipeline never reads. Inputs/outputs are CAPPED summaries; the uncapped payload stays
/// in the source store and is re-fetched on demand by provenance (agent + session + call_id).
#[derive(Debug, Clone)]
pub struct Event {
    /// Source agent: "claude", "codex", "opencode", "antigravity".
    pub agent: &'static str,
    /// Session this event belongs to (the PARENT session for subagent events).
    pub session: String,
    /// Epoch milliseconds; 0 if unknown.
    pub ts: i64,
    /// "tool" | "subagent_start" | "subagent_result".
    pub kind: &'static str,
    /// Tool name ("Bash", "shell", "run_command") or subagent title/sender.
    pub name: String,
    /// Compact human-meaningful input summary (command line, file path, prompt), capped.
    pub input: String,
    /// Tool output / subagent result, capped.
    pub output: String,
    /// Success if the store recorded one (is_error / status / exit hints); None when unknown.
    pub ok: Option<bool>,
    /// The store's own correlation id (tool_use id / call_id / callID). Synthesized
    /// (unique within the session) when the store has none. Dedupe + raw-fetch key.
    pub call_id: String,
    /// Subagent events: the child's own session id when it is independently viewable.
    pub child_session: String,
}
