//! Search output: stable machine rows and frozen, byte-bounded agent pages.
use crate::{
    data::DataRoot,
    pages,
    query::{Filters, Hit, Results},
    surface,
};
use anyhow::Result;
use serde_json::{json, Value};
use std::io::IsTerminal;
#[derive(Default)]
pub struct Style {
    pub json: bool,
    pub flat: bool,
    pub compact: bool,
    pub count: bool,
    pub tiers: bool,
    pub chats: bool,
    pub semantic: bool,
    pub checked: bool,
    pub color: bool,
    pub full_argv: Vec<String>,
    pub data_root: Option<DataRoot>,
    pub requested: Option<usize>,
    pub diversify: bool,
    pub caller_unknown: bool,
    pub self_exclusion: Value,
    pub filter_coverage: Value,
    pub pending_sources: usize,
}
fn speaker_legend(style: &Style, terminal: bool) -> Option<&'static str> {
    (terminal
        && style.color
        && !style.compact
        && !style.flat
        && !style.json
        && !style.count
        && !style.tiers)
        .then_some("key: › you · → agent · ⇒ subagent · ⚙ tool")
}
pub fn safe(s: &str) -> String {
    surface::safe(s, false)
}
pub fn project(s: &str) -> String {
    crate::history::project_leaf(s)
}
pub fn age(ts: i64) -> String {
    if ts <= 0 {
        return "-".into();
    }
    let n = (chrono::Utc::now().timestamp_millis() - ts).max(0) / 1000;
    if n < 3600 {
        format!("{}m", n / 60)
    } else if n < 86400 {
        format!("{}h", n / 3600)
    } else if n < 86400 * 365 {
        format!("{}d", n / 86400)
    } else {
        format!("{}y", n / (86400 * 365))
    }
}
pub fn checked(no_auto: bool) -> bool {
    !no_auto && !crate::data::read_only()
}
pub fn freshness(checked: bool, pending: usize) -> Value {
    let mut v = if checked {
        json!({"state":"no-known-failure","failing":false,"checked":true})
    } else {
        json!({"state":"unchecked","failing":false,"checked":false})
    };
    if pending > 0 {
        v["pending_sources"] = json!(pending)
    }
    v
}
pub fn hit_value(h: &Hit) -> Value {
    let mut value = json!({"session":h.session,"agent":h.agent,"project":h.project,"model":h.model,"model_source":if h.who=="tool"{"tool"}else{&h.model_source},"turn":h.turn,"ts":h.ts,"who":h.who,"snippet":h.snippet,"score":h.score,"handle":h.handle,"self":h.self_marker});
    if h.matched != "phrase" {
        value["matched"] = json!(h.matched)
    }
    if h.matched == "semantic" {
        value["lane"] = json!("semantic");
        value["sem_score"] = json!(h.score)
    }
    value
}
pub fn compact_line(h: &Hit) -> String {
    let quality = if h.matched == "semantic" {
        if h.score < 0.84 {
            "~semantic-weak (meaning match)"
        } else {
            "~semantic (meaning match)"
        }
    } else if h.matched != "phrase" {
        "~all"
    } else if ["partial", "interior"].contains(&h.boundary_class.as_str()) {
        "~substr"
    } else {
        ""
    };
    let snippet = surface::safe(&h.snippet, false);
    [
        h.handle.clone(),
        format!("{}/{}", safe(&h.agent), safe(&h.who)),
        safe(&project(&h.project)),
        age(h.ts),
        if h.self_marker {
            "~self".into()
        } else {
            String::new()
        },
        quality.into(),
        snippet,
    ]
    .into_iter()
    .filter(|s| !s.is_empty())
    .collect::<Vec<_>>()
    .join(" ")
}
pub fn emit(r: &Results, q: &str, f: &Filters, s: &Style) -> Result<()> {
    emit_semantic(r, q, f, s, None)
}
pub fn emit_semantic(
    r: &Results,
    q: &str,
    _f: &Filters,
    s: &Style,
    coverage: Option<&crate::semantic::Coverage>,
) -> Result<()> {
    let truncated = ((!s.count && !s.tiers) && r.hits.len() < r.total) || r.total_basis != "exact";
    let mut out = String::new();
    if s.count {
        out = format!(
            "{}{}\n",
            if r.total_basis == "exact" { "" } else { ">=" },
            r.total
        );
    } else if s.tiers {
        out = [
            "phrase_aligned",
            "phrase_partial",
            "phrase_interior",
            "all_terms",
            "total",
        ]
        .map(|name| format!("{name}={}", r.tiers.get(name).copied().unwrap_or(0)))
        .join(" ")
            + "\n";
    } else if s.json {
        let mut meta = json!({"kind":"agrep-meta","query":q,"engine":"sqlite","freshness":freshness(s.checked,s.pending_sources),"filter_coverage":s.filter_coverage,"self_exclusion":s.self_exclusion,"completeness":{"shown":r.hits.len(),"total":r.total,"total_basis":r.total_basis,"unit":if s.chats || s.semantic{"chat"}else{"matching row"},"truncated":truncated,"tool_rows":r.tool_rows},"semantic_coverage":null});
        if s.filter_coverage.is_null() {
            meta["filter_coverage"] = json!({"checked":true,"empty_dimensions":[]})
        }
        if s.self_exclusion.is_null() {
            meta["self_exclusion"] = json!({"active":false,"reason":"machine-surface"})
        }
        if let Some(coverage) = coverage {
            meta["semantic_coverage"] = serde_json::to_value(coverage)?;
            meta["semantic_partial"] = json!(coverage.partial());
            meta["engine"] = json!(if s.semantic {
                "semantic"
            } else if r.hits.iter().any(|h| h.matched == "semantic") {
                "sqlite+semantic"
            } else {
                "sqlite"
            });
        }
        if truncated {
            meta["completeness"]["more_argv"] = json!(s.full_argv);
            meta["completeness"]["more_command"] = json!(surface::shell(&s.full_argv));
            meta["completeness"]["more_kind"] = json!("broader-live-rerun");
            meta["completeness"]["full_argv"] = json!(s.full_argv);
            meta["completeness"]["full_command"] = json!(surface::shell(&s.full_argv));
        }
        if r.tools_excluded {
            meta["completeness"]
                .as_object_mut()
                .unwrap()
                .remove("tool_rows");
            meta["completeness"]["tools_excluded"] = json!(true);
        } else if r.total_basis != "exact" {
            meta["completeness"]
                .as_object_mut()
                .unwrap()
                .remove("tool_rows");
            meta["completeness"]["tool_rows_basis"] = json!("unknown");
        }
        if r.hits.is_empty() {
            meta["hits"] = json!([])
        }
        out += &format!("{}\n", crate::json_output::to_string(&meta)?);
        for h in &r.hits {
            let mut value = hit_value(h);
            if s.caller_unknown {
                value["self"] = Value::Null;
            }
            out += &format!("{}\n", crate::json_output::to_string(&value)?);
        }
    } else if s.flat {
        for h in &r.hits {
            out += &format!(
                "{}\t{}\t{}\t{}\t{}\t{}\n",
                safe(&h.session),
                safe(&h.agent),
                safe(&project(&h.project)),
                h.turn,
                safe(&h.who),
                highlight(&safe(&h.snippet), q, s.color)
            );
        }
    } else if s.compact {
        let mut ordered = r.hits.clone();
        if s.requested.is_none() {
            let mut seen = std::collections::HashSet::new();
            let (front, tail): (Vec<_>, Vec<_>) = ordered
                .into_iter()
                .partition(|h| seen.insert((h.agent.clone(), h.family.clone())));
            ordered = front.into_iter().chain(tail).collect();
        }
        let frozen = pages::Frozen {
            records: ordered
                .iter()
                .map(|h| pages::Record {
                    line: compact_line(h),
                    family: format!("{}:{}", h.agent, h.family),
                    score: h.score,
                    lane: h.matched.clone(),
                    tool: h.who == "tool",
                    identity: if h.who == "tool" {
                        h.handle.clone()
                    } else {
                        format!("{}:{}:{}", h.agent, h.session, h.turn)
                    },
                })
                .collect(),
            query: q.into(),
            total: r.total,
            exact: r.total_basis == "exact",
            shown: 0,
            requested: s.requested,
            chat_scoped: _f.chat.is_some() || !s.diversify,
            full_argv: s.full_argv.clone(),
        };
        let page = pages::select(frozen);
        out = page.text;
        let more = if let Some(root) = &s.data_root {
            pages::save(root, &page.remainder)?
        } else {
            None
        };
        if more.is_some() || truncated || !page.remainder.records.is_empty() {
            eprintln!("{}", pages::footer(&page.remainder, more.as_deref()));
        }
    } else {
        let mut sessions = Vec::<(&str, Vec<&Hit>)>::new();
        for hit in &r.hits {
            if let Some((_, rows)) = sessions.iter_mut().find(|(id, _)| *id == hit.session) {
                rows.push(hit)
            } else {
                sessions.push((&hit.session, vec![hit]));
            }
        }
        for (i, (_, hits)) in sessions.iter().enumerate() {
            if i > 0 {
                out.push('\n');
            }
            let first = hits[0];
            let mut crumbs = format!(
                "{} · {}",
                safe(&first.agent),
                safe(&project(&first.project))
            );
            if first.side {
                crumbs.push_str(" · [side chat]");
            }
            if first.self_marker {
                crumbs.push_str(" · ~self");
            }
            let count = format!(
                "{} hit{}",
                hits.len(),
                if hits.len() == 1 { "" } else { "s" }
            );
            let tail = format!("{} · {} · {}", first.handle, count, age(first.ts));
            out += &format!("\x1b[1;36m{crumbs}\x1b[0m  \x1b[2m{tail}\x1b[0m\n");
            for h in hits {
                let mark = match h.who.as_str() {
                    "agent" => "→",
                    "subagent" => "⇒",
                    "tool" => "⚙",
                    _ => "›",
                };
                out += &format!(
                    "  \x1b[33m{:>4}\x1b[0m \x1b[2m{mark}\x1b[0m {}\x1b[2m{}\x1b[0m\n",
                    h.turn,
                    highlight(&safe(&h.snippet), q, true),
                    if h.self_marker { " ~self" } else { "" }
                );
            }
        }
    }
    surface::write(&out)?;
    if let Some(legend) = speaker_legend(s, std::io::stderr().is_terminal()) {
        eprintln!("{legend}");
    }
    if truncated && !s.json && !s.compact {
        eprintln!(
            "{}{} matches{}",
            surface::number(r.total),
            if r.total_basis == "exact" { "" } else { "+" },
            if r.total_basis == "exact" {
                ""
            } else {
                " (floor; -c exact)"
            }
        );
    }
    Ok(())
}
fn highlight(s: &str, q: &str, color: bool) -> String {
    if !color || q.is_empty() {
        return s.into();
    }
    let re = regex::RegexBuilder::new(&regex::escape(q))
        .case_insensitive(true)
        .build();
    match re {
        Ok(re) => re.replace_all(s, "\x1b[1;31m$0\x1b[0m").into_owned(),
        Err(_) => s.into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn semantic_rows_name_the_meaning_lane() {
        let hit = Hit {
            matched: "semantic".into(),
            session: "semantic-session".into(),
            ..Hit::default()
        };
        assert!(compact_line(&hit).contains("meaning match"));
    }
    #[test]
    fn legend_is_scoped_to_classic_terminals() {
        let mut style = Style {
            color: true,
            ..Style::default()
        };
        assert!(speaker_legend(&style, true).unwrap().contains("⚙ tool"));
        assert!(speaker_legend(&style, false).is_none());
        style.json = true;
        assert!(speaker_legend(&style, true).is_none());
    }
}
