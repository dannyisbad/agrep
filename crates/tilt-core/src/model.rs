//! The normalized unit every adapter emits.

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
    /// The human-authored text (Danny's words). Wrappers/tool-results already stripped.
    pub text: String,
    /// Model that produced the agent's side of this turn ("claude-opus-4-8",
    /// "gpt-5.3-codex-spark", "gemini-3.1-pro-preview"). Empty when the source omits it.
    pub model: String,
    /// The agent's reply to this turn, trimmed for display. Empty if none was captured.
    pub reply: String,
}
