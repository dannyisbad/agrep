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
        || policy
            .text_prefixes
            .iter()
            .any(|p| t.starts_with(p.as_str()))
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

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};
#[inline]
fn set_arc_str(value: &mut Arc<str>, desired: &str) {
    if value.as_ref() != desired {
        *value = Arc::from(desired);
    }
}

pub fn normalize_messages(msgs: Vec<Message>, harness: &HarnessPolicy) -> Vec<Message> {
    // row_kind is cheap per row: one sequential pass is faster and less scheduler-sensitive
    // than dispatching thousands of tiny rayon jobs.
    let kinds: Vec<RowKind> = msgs
        .iter()
        .map(|message| row_kind(message, harness))
        .collect();
    let needs_model_backfill = msgs.iter().zip(&kinds).any(|(m, kind)| {
        matches!(kind, RowKind::User | RowKind::Subagent) && m.model.trim().is_empty()
    });

    type SessionKey = (&'static str, Arc<str>);
    type ModelTimeline = Vec<(u32, Arc<str>)>;
    let mut session_models: HashMap<SessionKey, HashSet<Arc<str>>> = HashMap::new();
    let mut session_timeline: HashMap<SessionKey, ModelTimeline> = HashMap::new();
    // The common case is an adapter that already attached a model to every real row. In that
    // case no lookup below is possible, so avoid hashing/cloning every session and model just
    // to build two maps that would never be read.
    if needs_model_backfill {
        for (m, kind) in msgs.iter().zip(&kinds) {
            let model = m.model.trim();
            if model.is_empty() || !matches!(kind, RowKind::User | RowKind::Subagent) {
                continue;
            }
            session_models
                .entry((m.agent, m.session.clone()))
                .or_default()
                .insert(m.model.clone());
            session_timeline
                .entry((m.agent, m.session.clone()))
                .or_default()
                .push((m.turn, m.model.clone()));
        }
        for models in session_timeline.values_mut() {
            models.sort_by_key(|(turn, _)| *turn);
        }
    }

    msgs.into_iter()
        .zip(kinds)
        .map(|(mut m, kind)| {
            let raw_model_empty = m.model.trim().is_empty();
            let (who, model_source): (&str, &str) = match kind {
                RowKind::Synthetic => ("synthetic", "synthetic"),
                RowKind::Control => {
                    set_arc_str(&mut m.model, "<control>");
                    ("control", "control")
                }
                RowKind::Recap => {
                    set_arc_str(&mut m.model, "<recap>");
                    ("recap", "recap")
                }
                RowKind::Harness => {
                    if raw_model_empty {
                        set_arc_str(&mut m.model, "<harness>");
                        ("harness", "harness")
                    } else {
                        ("harness", "explicit_harness")
                    }
                }
                RowKind::User | RowKind::Subagent => {
                    // side turns are real work: same model attribution as user
                    // turns (their child sessions carry their own model pool)
                    let who = if kind == RowKind::Subagent {
                        "subagent"
                    } else {
                        "user"
                    };
                    if !raw_model_empty {
                        (who, "explicit")
                    } else {
                        let models = session_models.get(&(m.agent, m.session.clone()));
                        match models.map(|s| s.len()).unwrap_or(0) {
                            1 => {
                                m.model = models
                                    .and_then(|s| s.iter().next())
                                    .cloned()
                                    .unwrap_or_else(|| Arc::from(""));
                                (who, "session")
                            }
                            0 => (who, "unknown"),
                            _ => {
                                let timeline = session_timeline.get(&(m.agent, m.session.clone()));
                                if let Some(model) =
                                    timeline.and_then(|rows| temporal_backfill(rows, m.turn))
                                {
                                    m.model = model;
                                    (who, "temporal_session")
                                } else {
                                    (who, "ambiguous_session")
                                }
                            }
                        }
                    }
                }
            };
            // Normalize in place. Parsed/cache rows already carry these labels in the common
            // explicit-user case, so this avoids rebuilding every Message and allocating two
            // short Arc strings per row. A changed classification still overwrites stale labels.
            set_arc_str(&mut m.who, who);
            set_arc_str(&mut m.model_source, model_source);
            m
        })
        .collect()
}

fn temporal_backfill(rows: &[(u32, Arc<str>)], turn: u32) -> Option<Arc<str>> {
    let before = rows
        .iter()
        .rev()
        .find(|(known_turn, _)| *known_turn < turn)
        .map(|(_, model)| model);
    let after = rows
        .iter()
        .find(|(known_turn, _)| *known_turn > turn)
        .map(|(_, model)| model);
    match (before, after) {
        (Some(a), Some(b)) if a == b => Some(a.clone()),
        (None, Some(model)) => Some(model.clone()),
        (Some(model), None) => Some(model.clone()),
        _ => None,
    }
}
