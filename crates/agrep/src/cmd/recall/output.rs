use super::*;
/// A block's head: handle · agent · project · when · meaning label · provenance marks.
pub(super) fn head(h: &Hit) -> String {
    let meaning = if h.matched == "semantic" && h.sem_weak {
        "weak meaning match"
    } else if h.matched == "semantic" {
        "meaning match"
    } else {
        ""
    };
    let marks = [
        h.self_marker.then_some("~self"),
        h.meta_marker.then_some("~meta"),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join(" ");
    [
        surface::safe(&h.handle, false),
        surface::safe(&h.agent, false),
        surface::safe(&render::project(&h.project), false),
        surface::timestamp(h.ts),
        meaning.into(),
        marks,
    ]
    .into_iter()
    .filter(|field| !field.is_empty())
    .collect::<Vec<_>>()
    .join(" · ")
}
/// 0.4.9's disclosure that an explicit handle bypassed the supplied filters, naming those
/// whose own predicates would have excluded it (py/recall.py:1540-1574,
/// py/surface_policy.py:2424-2432).
pub(super) fn handle_filter_note(
    flags: &[&str],
    f: &agrep_core::query::Filters,
    w: &Window,
    ts: i64,
) -> String {
    use agrep_core::history::project_matches;
    let mut mismatched = Vec::new();
    if f.chat.as_deref().is_some_and(|chat| chat != w.session) {
        mismatched.push("--chat");
    }
    if f.agent
        .as_deref()
        .is_some_and(|agent| !w.agent.to_lowercase().contains(&agent.to_lowercase()))
    {
        mismatched.push("--agent");
    }
    if f.project
        .as_deref()
        .is_some_and(|p| !project_matches(&w.project, p))
    {
        mismatched.push("--project");
    }
    if f.exclude_project
        .as_deref()
        .is_some_and(|p| project_matches(&w.project, p))
    {
        mismatched.push("--exclude-project");
    }
    if f.since.is_some_and(|since| ts < since) {
        mismatched.push("--since");
    }
    if f.until.is_some_and(|until| ts >= until) {
        mismatched.push("--until");
    }
    let mut line = format!(
        "explicit result handle wins: {} not applied",
        flags.join(", ")
    );
    if !mismatched.is_empty() {
        line += &format!(" ({} would have excluded it)", mismatched.join(", "));
    }
    line
}
/// 0.4.9's envelope fields for the queries whose meaning lane ran (py/recall.py:2169-2238,
/// 2572-2582): one query's status or the aggregate with the per-query list, and `hybrid`
/// when a meaning row made the page; `reduce` keeps only the status of a keyword page whose
/// meaning rows all lost the merge.
pub(super) fn semantic_meta(
    runs: &[(String, Value, Option<semantic::Coverage>)],
    multi: bool,
    visible: bool,
    reduce: bool,
) -> serde_json::Map<String, Value> {
    let mut meta = serde_json::Map::new();
    let Some((_, first, _)) = runs.first() else {
        return meta;
    };
    let all = |key: &str| runs.iter().all(|(_, s, _)| s[key] == true);
    // A refused query retains its cosine score kind (py/recall.py:2283-2291).
    let kind = |s: &Value| s["score_kind"].as_str().unwrap_or("cosine").to_owned();
    let kinds: BTreeSet<_> = runs.iter().map(|(_, s, _)| kind(s)).collect();
    let coverage = runs.iter().rev().find_map(|(_, _, c)| c.as_ref());
    meta.insert(
        "score_kind".into(),
        json!(if kinds.len() == 1 {
            kind(first)
        } else {
            "mixed".into()
        }),
    );
    // 0.4.9 left a lane's null coverage to the envelope's trailing field
    // (py/recall.py:2725-2729).
    if let Some(coverage) = coverage {
        meta.insert("semantic_coverage".into(), json!(coverage));
    }
    let status = if runs.len() == 1 {
        first.clone()
    } else {
        let states: BTreeSet<_> = runs.iter().map(|(_, s, _)| s["state"].as_str()).collect();
        let mut status = json!({"state":if states.len() == 1 {first["state"].clone()} else {json!("mixed")},"complete":all("complete"),"fallback_recommended":all("fallback_recommended")});
        let reasons: BTreeSet<_> = runs
            .iter()
            .filter(|(_, s, _)| s["fallback_recommended"] == true || s["state"] == "unavailable")
            .filter_map(|(_, s, _)| s["reason"].as_str().filter(|r| !r.is_empty()))
            .collect();
        if reasons.len() == 1 {
            status["reason"] = json!(reasons.first());
        }
        if !first["progress"].is_null()
            && runs
                .iter()
                .all(|(_, s, _)| s["progress"] == first["progress"])
        {
            status["progress"] = first["progress"].clone();
        }
        status
    };
    meta.insert("semantic_status".into(), status);
    if multi {
        meta.insert(
            "semantic_queries".into(),
            runs.iter()
                .map(|(query, status, coverage)| json!({"query":query,"status":status,"coverage":coverage}))
                .collect(),
        );
    }
    if visible {
        meta.insert("hybrid".into(), json!(true));
    } else if reduce {
        meta.retain(|key, _| key == "semantic_status");
    }
    meta
}
