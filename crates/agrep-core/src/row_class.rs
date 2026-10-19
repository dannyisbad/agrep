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

/// The corpus-local harness policy (`<data dir>/harness_prefixes.txt`): one entry per line,
/// blank lines and `#` comments skipped. A plain line is a prompt-text prefix; a
/// `project:` line names a project path segment prefix (ASCII case-insensitive) whose
/// chats are all harness traffic. Nothing is built in: a project name alone never
/// reclassifies a user's own turns unless the user configured it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct HarnessPolicy {
    pub text_prefixes: Vec<String>,
    pub project_prefixes: Vec<String>,
}

impl HarnessPolicy {
    pub fn parse(body: &str) -> Self {
        let mut policy = HarnessPolicy::default();
        for line in body.lines().map(str::trim) {
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            match line.strip_prefix("project:") {
                Some(segment) if !segment.trim().is_empty() => {
                    policy.project_prefixes.push(segment.trim().to_string());
                }
                Some(_) => {}
                None => policy.text_prefixes.push(line.to_string()),
            }
        }
        policy
    }
}

pub fn is_harness_project(project: &str, project_prefixes: &[String]) -> bool {
    !project_prefixes.is_empty()
        && project.split(['/', '\\']).any(|segment| {
            project_prefixes
                .iter()
                .any(|prefix| starts_ascii_case_insensitive(segment, prefix))
        })
}

pub fn is_subagent_turn(m: &Message) -> bool {
    // the adapter proved provenance (side sessions), or the codex handoff prefix
    // did (also covers cache rows written without the side flag)
    let t = m.text.trim_start();
    m.side || t.starts_with("[subagent task]") || t.starts_with("[subagent message]")
}

/// Harness traffic is opt-in: only the corpus-local policy marks it. agrep's own injected
/// text (compaction recaps, recovery blocks) is classified structurally by the adapters,
/// never by prompt prefixes, so no built-in prefix list is needed here.
pub fn is_harness_turn(project: &str, text: &str, policy: &HarnessPolicy) -> bool {
    let t = text.trim_start();
    is_harness_project(project, &policy.project_prefixes)
        || policy.text_prefixes.iter().any(|p| t.starts_with(p.as_str()))
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

pub fn row_kind(m: &Message, policy: &HarnessPolicy) -> RowKind {
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
    } else if is_harness_turn(&m.project, &m.text, policy) {
        RowKind::Harness
    } else if is_subagent_turn(m) {
        // after harness: a benchmark-project side turn is still benchmark noise
        RowKind::Subagent
    } else {
        RowKind::User
    }
}
