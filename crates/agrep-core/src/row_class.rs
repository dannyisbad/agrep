//! Normalize-pass row classification: one owned artifact shared by the CLI's
//! whole-corpus normalize and the streamed `--emit-rows` lane, so a streamed
//! row's `who` is byte-identical to what the finished index will publish.

use crate::model::Message;

pub fn is_synthetic_turn(model: &str) -> bool {
    model == "<synthetic>"
}

pub fn is_control_turn(text: &str) -> bool {
    let t = text.trim();
    t.eq_ignore_ascii_case("continue")
        || t == "Request interrupted by user"
        || t.starts_with("[Request interrupted by user")
}

#[inline]
fn starts_ascii_case_insensitive(value: &str, prefix: &str) -> bool {
    value
        .as_bytes()
        .get(..prefix.len())
        .is_some_and(|head| head.eq_ignore_ascii_case(prefix.as_bytes()))
}

pub fn is_harness_project(project: &str) -> bool {
    project.split(['/', '\\']).any(|segment| {
        segment.eq_ignore_ascii_case("vo-exp")
            || starts_ascii_case_insensitive(segment, "_probe")
            || starts_ascii_case_insensitive(segment, "control_")
            || starts_ascii_case_insensitive(segment, "run_control")
            || starts_ascii_case_insensitive(segment, "haiku-control")
            || starts_ascii_case_insensitive(segment, "haiku-treatment")
    })
}

pub fn is_subagent_turn(m: &Message) -> bool {
    // the adapter proved provenance (side sessions), or the codex handoff prefix
    // did (also covers cache rows written without the side flag)
    let t = m.text.trim_start();
    m.side || t.starts_with("[subagent task]") || t.starts_with("[subagent message]")
}

pub fn is_harness_turn(project: &str, text: &str, local_prefixes: &[String]) -> bool {
    let t = text.trim_start();
    is_harness_project(project)
        || local_prefixes.iter().any(|p| t.starts_with(p.as_str()))
        || t.starts_with("Return only valid JSON that matches the schema below.")
        || t.starts_with("You are a workflow planner for OpenCode.")
        || t.starts_with("You are an independent verifier.")
        || t.starts_with("\"Create a file named ok.txt")
        || t.starts_with("\"You are working in the current directory.")
        || t.starts_with("\"Stay in this directory. STEP 1:")
        || t.starts_with("\"STEP 1: run ")
        || t.starts_with("Follow the README in this directory to run the analysis")
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum RowKind {
    User,
    Subagent,
    Synthetic,
    Control,
    Recap,
    Harness,
}

impl RowKind {
    pub fn who(self) -> &'static str {
        match self {
            RowKind::User => "user",
            RowKind::Subagent => "subagent",
            RowKind::Synthetic => "synthetic",
            RowKind::Control => "control",
            RowKind::Recap => "recap",
            RowKind::Harness => "harness",
        }
    }
}

pub fn row_kind(m: &Message, harness_prefixes: &[String]) -> RowKind {
    let raw_model = m.model.trim();
    if m.who.as_ref() == "recap" {
        // Structural: the adapter proved a compaction boundary. A propagated
        // "<synthetic>" model marker or control-shaped text must not
        // reclassify it - postcompact resolves boundaries by this row.
        RowKind::Recap
    } else if is_synthetic_turn(raw_model) {
        RowKind::Synthetic
    } else if is_control_turn(&m.text) {
        RowKind::Control
    } else if is_harness_turn(&m.project, &m.text, harness_prefixes) {
        RowKind::Harness
    } else if is_subagent_turn(m) {
        // after harness: a benchmark-project side turn is still benchmark noise
        RowKind::Subagent
    } else {
        RowKind::User
    }
}
