//! Root-only pre-compaction evidence packet, bounded in both representations.
use crate::{
    caller::Caller,
    evidence::{self, Row},
    handles, surface,
};
use anyhow::{bail, Result};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::BTreeSet;
fn authority() -> Value {
    json!({"role":"supplement","primary":["visible_user_turns","platform_compact_summary"],"newer_visible_state_wins":true,"semantic_search_performed":false})
}
pub fn failure(status: &str, reason: &str) -> Value {
    json!({"kind":"agrep-postcompact","schema_version":1,"status":status,"reason":reason,"authority":authority(),"implicit_widening":false})
}
fn clipped(text: &str, limit: usize) -> (String, usize) {
    if text.len() <= limit {
        return (text.into(), 0);
    }
    let head = surface::prefix(text, (limit.saturating_sub(70)) * 38 / 100);
    let mut start = text
        .len()
        .saturating_sub((limit.saturating_sub(70)) * 62 / 100);
    while !text.is_char_boundary(start) {
        start += 1
    }
    let tail = &text[start..];
    let omitted = text.len() - head.len() - tail.len();
    (
        format!(
            "{head}\n[… {} UTF-8 bytes omitted …]\n{tail}",
            surface::number(omitted)
        ),
        omitted,
    )
}
pub fn packet(c: &Connection, caller: &Caller, boundary_ms: Option<i64>) -> Result<Value> {
    if !caller.resolved {
        bail!("the calling session is not indexed")
    }
    let boundary = if let Some(ts) = boundary_ms {
        c.query_row("SELECT max(position) FROM prose WHERE visible=1 AND session=? AND who='recap' AND ts=?",params![caller.session,ts],|r|r.get::<_,Option<i64>>(0))?
    } else {
        caller.boundary
    };
    let Some(boundary) = boundary else {
        bail!("no structural compaction boundary is indexed for this caller")
    };
    let mut upper = boundary;
    let mut fallbacks = 0;
    let (previous, rows) = loop {
        let lower:Option<i64>=c.query_row("SELECT max(position) FROM prose WHERE visible=1 AND session=? AND who='recap' AND position<?",params![caller.session,upper],|r|r.get(0))?;
        let rows = evidence::load_rows(c, &caller.session, lower.unwrap_or(0), upper - 1)?
            .into_iter()
            .filter(|r| ["user", "agent"].contains(&r.who.as_str()) && !r.text.is_empty())
            .collect::<Vec<_>>();
        if !rows.is_empty() || lower.is_none() {
            break (lower, rows);
        }
        upper = lower.unwrap();
        fallbacks += 1;
    };
    let turns: BTreeSet<_> = rows.iter().map(|r| r.turn).collect();
    let eligible = turns.len();
    let selected: BTreeSet<_> = turns.iter().rev().take(8).copied().collect();
    let mut chosen: Vec<&Row> = rows.iter().filter(|r| selected.contains(&r.turn)).collect();
    let project: String = c.query_row(
        "SELECT project FROM sessions WHERE id=?",
        [&caller.session],
        |r| r.get(0),
    )?;
    loop {
        let budget = (5000 / chosen.len().max(1)).clamp(320, 2400);
        let mut omitted = 0;
        let output:Vec<_>=chosen.iter().map(|r|{let(text,n)=clipped(&r.text,budget);omitted+=n;json!({"session":r.session,"turn":r.turn,"ts":r.ts,"who":r.who,"tier":"root","kind":"message","handle":handles::mint_full(&r.session,r.turn,&r.text),"text":text,"render_truncated":n>0,"render_omitted_bytes":n,"source_truncated":r.source_chars>r.text.chars().count(),"source_truncation_state":"recorded"})}).collect();
        let shown = chosen.iter().map(|r| r.turn).collect::<BTreeSet<_>>().len();
        let next = output
            .last()
            .map(|r| json!(["agrep", "around", r["handle"].as_str().unwrap(), "-C", "2"]));
        let value = json!({"kind":"agrep-postcompact","schema_version":1,"status":if output.is_empty(){"empty"}else{"recovered"},"authority":authority(),"selection":{"caller_session":caller.session,"family_root":caller.root,"family_resolved":true,"scope":"root-only","boundary_turn":boundary,"previous_boundary_turn":previous,"boundary_basis":"indexed_structural_recap","window_fallbacks":fallbacks,"window_source":"caller","tools":"excluded","delegated_sessions":"excluded","project":project,"agent":caller.agent,"model":chosen.last().map(|r|&r.model),"model_source":chosen.last().map(|r|&r.model_source)},"coverage":{"family":"complete","boundary":"complete","eligible_root_blocks":eligible,"shown_root_blocks":shown,"budget_bytes":8000,"json_budget_bytes":16000,"budget_truncated":shown<eligible||omitted>0,"render_omitted_bytes":omitted,"source_truncated_rows":chosen.iter().filter(|r|r.source_chars>r.text.chars().count()).count(),"source_truncation_state":"recorded"},"rows":output,"omissions":{"root_blocks":eligible-shown,"tools":"policy_excluded","delegated_sessions":"policy_excluded"},"next":{"around_argv":next},"implicit_widening":false});
        if crate::json_output::to_string(&value)?.len() < 16000 && human(&value).len() < 8000 {
            return Ok(value);
        }
        let Some(oldest) = chosen.first().map(|r| r.turn) else {
            bail!("postcompact metadata exceeds its byte contract")
        };
        if shown <= 1 {
            bail!("postcompact metadata exceeds its byte contract")
        };
        chosen.retain(|r| r.turn != oldest);
    }
}
pub fn human(packet: &Value) -> String {
    let s = &packet["selection"];
    let cv = &packet["coverage"];
    let mut lines=vec!["postcompact: supplement to the visible compact summary; newer visible user text wins".to_owned(),format!("caller @{} · root @{} · {} · project {} (orientation only)",surface::safe(s["caller_session"].as_str().unwrap_or(""),false),surface::safe(s["family_root"].as_str().unwrap_or(""),false),surface::safe(s["agent"].as_str().unwrap_or(""),false),surface::safe(s["project"].as_str().unwrap_or(""),false)),format!("boundary turn {} (structural recap) · root-only · tools and delegated sessions excluded",s["boundary_turn"]),String::new(),"── recent root/main tail · newest blocks selected, chronological render ──".into()];
    for r in packet["rows"].as_array().unwrap() {
        lines.push(format!(
            "{} {} {}: {}",
            r["turn"],
            r["who"].as_str().unwrap(),
            r["handle"].as_str().unwrap(),
            surface::safe(r["text"].as_str().unwrap(), true)
        ));
    }
    lines.push(String::new());
    lines.push(format!("coverage: {}/{} eligible root blocks shown; {} older blocks omitted; tools and delegated sessions excluded",cv["shown_root_blocks"],cv["eligible_root_blocks"],packet["omissions"]["root_blocks"]));
    if let Some(args) = packet["next"]["around_argv"].as_array() {
        lines.push(format!(
            "deepen exact nearby context: {}",
            args.iter()
                .map(|a| a.as_str().unwrap_or(""))
                .collect::<Vec<_>>()
                .join(" ")
        ));
    }
    lines.push("only if one specific different-history detail remains: agrep recall \"<missing detail + clues>\" --hits 2 --budget 5000".into());
    lines.join("\n") + "\n"
}
