//! Search output: stable machine rows and frozen, byte-bounded agent pages.
use crate::{
    boundary_rank,
    data::DataRoot,
    evidence::{content_terms, py_space},
    pages,
    query::{Filters, Hit, Results},
    surface,
    window::Snip,
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
    /// The query is a regular expression (`-E`) or a whole word (`-w`).
    pub regex: bool,
    pub word: bool,
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
    /// Each `--chats` hit's matching rows in its chat (0.4.9's `session_hits`).
    pub session_hits: Vec<usize>,
}
/// 0.4.9's `_chat_head`: a chat's agent, project and marks, then its handle, matching
/// rows and age (omitted when unknown); bracketed without color.
fn chat_head(first: &Hit, n: usize, color: bool) -> String {
    let mut crumbs = format!(
        "{} · {}",
        safe(&first.agent),
        safe(&project(&first.project))
    );
    if first.side || first.who == "subagent" {
        crumbs.push_str(" · [side chat]");
    }
    if first.self_marker {
        crumbs.push_str(" · ~self");
    }
    let mut tail = format!(
        "{} · {n} hit{}",
        safe(&first.handle),
        if n == 1 { "" } else { "s" }
    );
    let age = age(first.ts);
    if age != "-" {
        tail += &format!(" · {age}");
    }
    if color {
        format!("\x1b[1;36m{crumbs}\x1b[0m  \x1b[2m{tail}\x1b[0m\n")
    } else {
        format!("{crumbs}  [{tail}]\n")
    }
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
/// 0.4.9's freshness-story line for an absence read with `--no-auto`.
pub const NO_AUTO_STALE: &str =
    "history may be stale: automatic freshness checks are disabled by --no-auto";
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
/// 0.4.9's snippet patterns: what re-finds a hit's match in its snippet, to paint it
/// (`_hl`) and to center the compact page's narrower window (`_compact_snippet`).
pub struct Marks {
    /// `_match_pat`: the phrase with `[\W_]*` between its tokens, a whole word, or the
    /// user's regex; none for a meaning-only result.
    pattern: Option<fancy_regex::Regex>,
    /// `_terms_hl_pat`: each content term, for keyword rows matched in any order.
    terms: Option<fancy_regex::Regex>,
    /// The query's boundary terms, which place an any-order row's matches.
    prepared: boundary_rank::PreparedQuery,
}
impl Marks {
    pub fn new(q: &str, s: &Style) -> Self {
        let build = |pattern: String| {
            let mut b = fancy_regex::RegexBuilder::new(&format!("(?i:{pattern})"));
            b.backtrack_limit(100_000);
            b.build().ok()
        };
        let keyword = !s.semantic && !s.regex && !s.word;
        let pattern = if s.semantic {
            None
        } else if s.regex {
            build(q.to_owned())
        } else if s.word {
            build(format!(r"(?<!\w)(?:{})(?!\w)", regex::escape(q)))
        } else {
            let tokens: Vec<String> = q
                .split(|c: char| py_space(c) || c == '-' || c == '_')
                .filter(|t| !t.is_empty())
                .map(regex::escape)
                .collect();
            (!tokens.is_empty())
                .then(|| build(tokens.join(r"[\W_]*")))
                .flatten()
        };
        let mut variants: Vec<String> = content_terms(q)
            .iter()
            .filter(|t| t.chars().count() >= 3)
            .flat_map(|t| boundary_rank::term_variants(t))
            .collect();
        variants.sort_by(|a, b| {
            b.chars()
                .count()
                .cmp(&a.chars().count())
                .then_with(|| a.cmp(b))
        });
        variants.dedup();
        let terms = (keyword && !variants.is_empty())
            .then(|| {
                build(
                    variants
                        .iter()
                        .map(|v| regex::escape(v))
                        .collect::<Vec<_>>()
                        .join("|"),
                )
            })
            .flatten();
        Self {
            pattern,
            terms,
            prepared: boundary_rank::prepare_query(q, &boundary_rank::Stats::new()),
        }
    }
    /// `_hl` with `_row_hl_pat`: the sanitized snippet with each match painted.
    fn paint(&self, h: &Hit, color: bool) -> String {
        let snippet = safe(&h.snippet);
        let pattern = match &self.terms {
            Some(terms) if ["all-terms", "content-terms"].contains(&h.matched.as_str()) => {
                Some(terms)
            }
            _ => self.pattern.as_ref(),
        };
        match pattern.filter(|_| color) {
            Some(p) => p
                .try_replacen(&snippet, 0, |m: &fancy_regex::Captures| {
                    format!("\x1b[1;31m{}\x1b[0m", &m[0])
                })
                .map_or_else(|_| snippet.clone(), |s| s.into_owned()),
            None => snippet,
        }
    }
    /// `_compact_snippet`: the snippet narrowed to 32 characters around its matches.
    fn compact(&self, h: &Hit) -> String {
        let text = h.snippet.as_str();
        let spans: Vec<std::ops::Range<usize>> = if h.matched == "all-terms" {
            let byte = |at: i64| {
                text.char_indices()
                    .nth(at as usize)
                    .map_or(text.len(), |(b, _)| b)
            };
            self.prepared
                .evaluate(text, None, true)
                .map(|score| {
                    score
                        .spans
                        .iter()
                        .flatten()
                        .map(|[start, end]| byte(*start)..byte(*end))
                        .collect()
                })
                .unwrap_or_default()
        } else {
            self.pattern
                .as_ref()
                .and_then(|p| p.find(text).ok().flatten())
                .map(|m| m.start()..m.end())
                .into_iter()
                .collect()
        };
        if spans.is_empty() {
            text.to_owned()
        } else {
            Snip::padded(text, &spans, 32).text
        }
    }
}
pub fn compact_line(h: &Hit, marks: &Marks) -> String {
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
    let snippet = surface::safe(&marks.compact(h), false);
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
        if h.meta_marker {
            "~meta".into()
        } else {
            String::new()
        },
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
    let marks = Marks::new(q, s);
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
        for (i, h) in r.hits.iter().enumerate() {
            let mut value = hit_value(h);
            if s.caller_unknown {
                value["self"] = Value::Null;
            }
            if let Some(n) = s.session_hits.get(i) {
                value["session_hits"] = json!(n);
            }
            out += &format!("{}\n", crate::json_output::to_string(&value)?);
        }
    } else if s.chats && !s.compact {
        // 0.4.9's `_emit_chats`: one head line per matching chat (`grep -l`).
        for (i, h) in r.hits.iter().enumerate() {
            let n = s.session_hits.get(i).copied().unwrap_or(1);
            out += &chat_head(h, n, s.color);
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
                marks.paint(h, s.color)
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
                    line: compact_line(h, &marks),
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
            // 0.4.9: a --no-meta page that dropped rows bounds only what it fetched.
            exhaustible: r.meta_dropped + r.meta_retained == 0,
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
            out += &chat_head(hits[0], hits.len(), true);
            for h in hits {
                let mark = match h.who.as_str() {
                    "agent" => "→",
                    "subagent" => "⇒",
                    "tool" => "⚙",
                    _ => "›",
                };
                out += &format!(
                    "  \x1b[33m{:>4}\x1b[0m \x1b[2m{mark}\x1b[0m {}\x1b[2m{}{}\x1b[0m\n",
                    h.turn,
                    marks.paint(h, true),
                    if h.self_marker { " ~self" } else { "" },
                    if h.meta_marker { " ~meta" } else { "" }
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
        assert!(compact_line(&hit, &Marks::new("", &Style::default())).contains("meaning match"));
    }
    #[test]
    fn compact_meaning_rows_narrow_their_whole_text_around_a_keyword() {
        let text = format!(
            "{} the lantern checklist {}",
            "a".repeat(400),
            "b".repeat(400)
        );
        let hit = Hit {
            matched: "semantic".into(),
            score: 0.9,
            snippet: text.clone(),
            text,
            session: "semantic-session".into(),
            ..Hit::default()
        };
        let line = compact_line(&hit, &Marks::new("lantern", &Style::default()));
        assert!(line.contains("lantern checklist"), "{line}");
        assert!(line.len() < 200, "{line}");
        let hit = Hit {
            snippet: "no keyword here".into(),
            ..hit
        };
        assert!(
            compact_line(&hit, &Marks::new("lantern", &Style::default()))
                .ends_with("no keyword here")
        );
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
