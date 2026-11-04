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
    pub no_auto: bool,
    pub color: bool,
    pub full_argv: Vec<String>,
    /// 0.4.9's bounded larger page and exhaustive rerun of this search, when it offered them.
    pub larger_argv: Option<Vec<String>>,
    pub exhaustive_argv: Option<Vec<String>>,
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
/// 0.4.9's local timezone label: the zone's name and UTC offset, as `CET (UTC+01:00)`
/// (py/summary.py:2023-2026).
pub fn timezone_label() -> String {
    let offset = chrono::Local::now().format("%z").to_string();
    let (hours, minutes) = offset.split_at(offset.len().min(3));
    format!(
        "{} (UTC{hours}:{minutes})",
        zone_name().unwrap_or_else(|| "local".into())
    )
}
#[cfg(unix)]
fn zone_name() -> Option<String> {
    // SAFETY: `localtime_r` fills the zeroed `tm` it is given, and `tm_zone`, when set,
    // points at a NUL-terminated name the C library keeps alive.
    unsafe {
        let now = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&now, &mut tm).is_null() || tm.tm_zone.is_null() {
            return None;
        }
        let name = std::ffi::CStr::from_ptr(tm.tm_zone)
            .to_string_lossy()
            .into_owned();
        (!name.is_empty()).then_some(name)
    }
}
#[cfg(windows)]
fn zone_name() -> Option<String> {
    use windows_sys::Win32::System::Time::{GetTimeZoneInformation, TIME_ZONE_INFORMATION};
    // GetTimeZoneInformation's documented TIME_ZONE_ID_DAYLIGHT.
    const TIME_ZONE_ID_DAYLIGHT: u32 = 2;
    // SAFETY: the zeroed structure is plain data that the call fills in.
    let mut info: TIME_ZONE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: `info` is a valid, writable TIME_ZONE_INFORMATION.
    let id = unsafe { GetTimeZoneInformation(&mut info) };
    let name = if id == TIME_ZONE_ID_DAYLIGHT {
        &info.DaylightName[..]
    } else {
        &info.StandardName[..]
    };
    let end = name.iter().position(|&c| c == 0).unwrap_or(name.len());
    let name = String::from_utf16_lossy(&name[..end]);
    (!name.is_empty()).then_some(name)
}
#[cfg(not(any(unix, windows)))]
fn zone_name() -> Option<String> {
    None
}
/// 0.4.9's freshness-story line for any read with `--no-auto` (py/search.py:7407-7416,
/// py/indexd_runtime.py:3378-3380).
pub const NO_AUTO_STALE: &str =
    "history may be stale: automatic freshness checks are disabled by --no-auto";
pub fn note_no_auto(no_auto: bool) {
    if no_auto {
        eprintln!("{NO_AUTO_STALE}");
    }
}
/// The machine freshness record; under `--no-auto` it is 0.4.9's: nothing checked the
/// sources, so the index may be stale.
pub fn machine_freshness(no_auto: bool, pending: usize) -> Value {
    if no_auto {
        json!({"state":"degraded","failing":true,"may_be_stale":true,"code":"search-index-stale","reason":"automatic freshness checks are disabled by --no-auto","checked":false})
    } else {
        freshness(checked(no_auto), pending)
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
/// A search hit's JSON row. A meaning row is 0.4.9's (py/search.py:1010-1038, py/ask.py:
/// 2613-2624): its cosine to six places, the keyword formula as `score`, no lane or match
/// label, and the empty concept and summary and unknown model source its message lane
/// reported.
pub fn hit_value(h: &Hit, now: i64) -> Value {
    if h.matched == "semantic" {
        return json!({"session":h.session,"agent":h.agent,"project":h.project,"concept":"","model":h.model,"model_source":"unknown","turn":h.turn,"ts":h.ts,"who":h.who,"sem_score":crate::window::round6(h.score),"score_kind":"cosine","snippet":h.snippet,"summary":"","semantic_source":"message","score":crate::query::meaning_score(h, now),"handle":h.handle,"self":h.self_marker});
    }
    let mut value = json!({"session":h.session,"agent":h.agent,"project":h.project,"model":h.model,"model_source":if h.who=="tool"{"tool"}else{&h.model_source},"turn":h.turn,"ts":h.ts,"who":h.who,"snippet":h.snippet,"score":h.score,"handle":h.handle,"self":h.self_marker});
    if h.matched != "phrase" {
        value["matched"] = json!(h.matched)
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
        if h.sem_weak {
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
    let exhaustible = !s.semantic && !r.hits.iter().any(|h| h.matched == "semantic");
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
        out = json_lines(
            r,
            q,
            s,
            coverage,
            truncated,
            chrono::Utc::now().timestamp_millis(),
        )?;
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
        let ordered = diversify(demote_meta(r.hits.clone()));
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
        for hit in demote_meta(r.hits.iter().collect()) {
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
    if truncated && !s.json && !s.compact && !s.count && !s.tiers {
        eprintln!("{}", completeness_line(r, s, exhaustible));
    }
    Ok(())
}
/// 0.4.9's `_demote_meta`: ~meta rows sink below lived rows, each side keeping its order.
fn demote_meta<T: std::borrow::Borrow<Hit>>(hits: Vec<T>) -> Vec<T> {
    let (lived, meta): (Vec<T>, Vec<T>) = hits.into_iter().partition(|h| !h.borrow().meta_marker);
    lived.into_iter().chain(meta).collect()
}
/// 0.4.9's compact order (py/search.py:5856-5884, py/compact.py:160-193): ~meta rows sunk,
/// then each quality lane led by one row per family, meaning rows after the first three
/// aligned phrase rows, and ~meta rows kept below lived ones.
fn diversify(hits: Vec<Hit>) -> Vec<Hit> {
    let one_lane = |rows: Vec<Hit>| -> Vec<Hit> {
        let mut seen = std::collections::HashSet::new();
        let (head, tail): (Vec<_>, Vec<_>) = rows.into_iter().partition(|h| {
            !h.family.is_empty() && seen.insert((h.agent.clone(), h.family.clone()))
        });
        head.into_iter().chain(tail).collect()
    };
    let (meaning, keyword): (Vec<_>, Vec<_>) =
        hits.into_iter().partition(|h| h.matched == "semantic");
    let (all_terms, phrase): (Vec<_>, Vec<_>) = keyword
        .into_iter()
        .partition(|h| matches!(h.matched.as_str(), "all-terms" | "content-terms"));
    let (phrase, meaning, all_terms) = (one_lane(phrase), one_lane(meaning), one_lane(all_terms));
    let ordered = if meaning.is_empty() {
        phrase.into_iter().chain(all_terms).collect()
    } else {
        let (mut aligned, interior): (Vec<_>, Vec<_>) = phrase
            .into_iter()
            .partition(|h| h.boundary_class != "interior");
        let rest = aligned.split_off(aligned.len().min(3));
        aligned
            .into_iter()
            .chain(meaning)
            .chain(rest)
            .chain(interior)
            .chain(all_terms)
            .collect()
    };
    demote_meta(ordered)
}
/// The JSON surface: one `agrep-meta` line, then one line per hit.
fn json_lines(
    r: &Results,
    q: &str,
    s: &Style,
    coverage: Option<&crate::semantic::Coverage>,
    truncated: bool,
    now: i64,
) -> Result<String> {
    let exhaustible = !s.semantic && !r.hits.iter().any(|h| h.matched == "semantic");
    let mut meta = json!({"kind":"agrep-meta","query":q,"engine":"sqlite","freshness":machine_freshness(s.no_auto,s.pending_sources),"filter_coverage":s.filter_coverage,"self_exclusion":s.self_exclusion,"completeness":{"shown":r.hits.len(),"total":r.total,"total_basis":r.total_basis,"unit":if s.chats || s.semantic{"chat"}else{"matching row"},"truncated":truncated,"tool_rows":r.tool_rows},"semantic_coverage":null});
    if s.filter_coverage.is_null() {
        meta["filter_coverage"] = json!({"checked":true,"empty_dimensions":[]})
    }
    if s.self_exclusion.is_null() {
        meta["self_exclusion"] = json!({"active":false,"reason":"machine-surface"})
    }
    if let Some(coverage) = coverage {
        meta["semantic_coverage"] = serde_json::to_value(coverage)?;
        meta["semantic_partial"] = json!(coverage.partial());
    }
    if s.semantic {
        // 0.4.9 named its meaning lane's engine and led with the lane's status
        // (py/search.py:4322, 8285-8287).
        meta["engine"] = json!("semantic:hybrid");
        meta["semantic"] = crate::semantic::status(coverage, !r.hits.is_empty());
    }
    if truncated {
        if let Some(argv) = &s.larger_argv {
            meta["completeness"]["more_command"] = json!(surface::shell(argv));
            meta["completeness"]["more_command_kind"] = json!("broader-rerun");
        }
        if let Some(argv) = &s.exhaustive_argv {
            meta["completeness"]["full_command"] = json!(surface::shell(argv));
        }
        if !exhaustible {
            meta["completeness"]["no_exhaustive_form"] = json!(NO_EXHAUSTIVE_FORM);
        }
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
    let mut out = format!("{}\n", crate::json_output::to_string(&meta)?);
    for (i, h) in r.hits.iter().enumerate() {
        let mut value = hit_value(h, now);
        if s.caller_unknown {
            value["self"] = Value::Null;
        }
        if let Some(n) = s.session_hits.get(i) {
            value["session_hits"] = json!(n);
        }
        out += &format!("{}\n", crate::json_output::to_string(&value)?);
    }
    Ok(out)
}
const NO_EXHAUSTIVE_FORM: &str =
    "the meaning lane ranks a bounded candidate set; no invocation returns every semantically related row";
/// 0.4.9's `completeness_line` (py/surface_policy.py:2257-2281): a cut page's rows, their
/// total and basis, and the larger page or why none exists.
fn completeness_line(r: &Results, s: &Style, exhaustible: bool) -> String {
    let exact = r.total_basis == "exact";
    let unit = if s.chats || s.semantic {
        "chat"
    } else {
        "matching row"
    };
    let tool = match r.tool_rows {
        n if n == 0 || !exact || r.tools_excluded => String::new(),
        n if unit.contains("row") => format!(" ({n} of them in tool output)"),
        1 => " (1 matching row is tool output)".into(),
        n => format!(" ({n} matching rows are tool output)"),
    };
    let basis = match (exact, exhaustible && !r.tools_excluded) {
        (true, _) => "",
        (false, true) => " (a floor, not the total; -c counts exactly)",
        (false, false) => " (a floor, not the total)",
    };
    let tail = match (&s.larger_argv, exhaustible) {
        (Some(argv), _) => format!(" · larger page: {}", surface::shell(argv)),
        (None, false) => format!(" · {NO_EXHAUSTIVE_FORM}"),
        (None, true) => String::new(),
    };
    format!(
        "showing {} of {}{} {unit}{}{tool}{basis}{tail}",
        r.hits.len(),
        r.total,
        if exact { "" } else { "+" },
        if r.total == 1 { "" } else { "s" }
    )
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
    // Expected lines are agrep 0.4.9's `-s "how do we rotate secret credentials" --json` on a
    // six-chat fixture with 24 embedded rows, without the keys PORTING.md lists as not
    // carried; `now` reproduces that run's scores.
    #[test]
    fn meaning_search_json_matches_0_4_9() {
        let row = |tail: &str, project: &str, ts: i64, cosine: f64, topic: &str, digest: &str| {
            let session = format!("01960000-0000-7000-8000-00000000{tail}");
            Hit {
                handle: format!("@{session}:0.{digest}"),
                session,
                agent: "pi".into(),
                project: project.into(),
                model: "pi/sonnet-5".into(),
                model_source: "explicit".into(),
                who: "agent".into(),
                ts,
                score: cosine,
                matched: "semantic".into(),
                snippet: format!(
                    "Here is the procedure for {topic}: the operator checks the inventory, \
                     updates the secret store, restarts the affected services and records \
                     the change."
                ),
                ..Hit::default()
            }
        };
        let mut r = Results::default();
        r.hits = vec![
            row(
                "f000",
                "/work/sec0",
                1792058906000,
                0.88186553,
                "rotating the release signing keys",
                "cb01",
            ),
            row(
                "f002",
                "/work/sec2",
                1792231706000,
                0.86953912,
                "replacing the deploy credentials in the vault",
                "f529",
            ),
            row(
                "f005",
                "/work/sec5",
                1792490906000,
                0.84503287,
                "revoking leaked ssh keys",
                "689b",
            ),
            row(
                "f003",
                "/work/sec3",
                1792318106000,
                0.83576804,
                "resetting the database passwords",
                "883d",
            ),
        ];
        r.total = 4;
        r.total_basis = "exact".into();
        let s = Style {
            json: true,
            semantic: true,
            session_hits: vec![1; 4],
            ..Style::default()
        };
        let coverage = crate::semantic::Coverage {
            embedded: 24,
            total: 24,
        };
        let q = "how do we rotate secret credentials";
        let lines: Vec<Value> = json_lines(&r, q, &s, Some(&coverage), false, 1793786928000)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        let full = json!({"complete":true,"fraction":1.0,"indexed":24,"order":"newest-first","pending":0,"total":24});
        assert_eq!(
            lines[0],
            json!({"completeness":{"shown":4,"tool_rows":0,"total":4,"total_basis":"exact","truncated":false,"unit":"chat"},"engine":"semantic:hybrid","filter_coverage":{"checked":true,"empty_dimensions":[]},"freshness":{"checked":true,"failing":false,"state":"no-known-failure"},"kind":"agrep-meta","query":q,"self_exclusion":{"active":false,"reason":"machine-surface"},"semantic":{"complete":true,"coverage":full,"eligible":true,"fallback_recommended":false,"min_score":0.82,"partial":false,"reason":null,"score_kind":"cosine","state":"ready"},"semantic_coverage":full,"semantic_partial":false})
        );
        assert_eq!(
            lines[1],
            json!({"agent":"pi","concept":"","handle":"@01960000-0000-7000-8000-00000000f000:0.cb01","model":"pi/sonnet-5","model_source":"unknown","project":"/work/sec0","score":0.2972,"score_kind":"cosine","self":false,"sem_score":0.881866,"semantic_source":"message","session":"01960000-0000-7000-8000-00000000f000","session_hits":1,"snippet":"Here is the procedure for rotating the release signing keys: the operator checks the inventory, updates the secret store, restarts the affected services and records the change.","summary":"","ts":1792058906000i64,"turn":0,"who":"agent"})
        );
        let scores: Vec<_> = lines[1..]
            .iter()
            .map(|v| (v["score"].clone(), v["sem_score"].clone()))
            .collect();
        assert_eq!(
            scores,
            [
                (0.2972, 0.881866),
                (0.3281, 0.869539),
                (0.3807, 0.845033),
                (0.3448, 0.835768)
            ]
            .map(|(score, sem)| (json!(score), json!(sem)))
        );
        // A meaning lane that cleared nothing: 0.4.9's zero-result status.
        let none = json_lines(
            &Results::default(),
            "zebra quantum lighthouse marmalade",
            &s,
            Some(&coverage),
            false,
            0,
        )
        .unwrap();
        let meta: Value = serde_json::from_str(none.trim()).unwrap();
        assert_eq!(meta["hits"], json!([]));
        assert_eq!(
            meta["semantic"],
            json!({"complete":true,"coverage":full,"eligible":true,"fallback_recommended":false,"min_score":0.82,"partial":false,"reason":null,"score_kind":"cosine","state":"no-confident-match"})
        );
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
    // Expected strings follow py/surface_policy.py:2257-2281 for a total the scan stopped
    // short of, with and without an exhaustive form.
    #[test]
    fn cut_page_line_states_a_floor_as_0_4_9() {
        let mut r = Results::default();
        r.hits = vec![Hit::default(); 40];
        r.total = 118;
        r.total_basis = "floor".into();
        let s = Style {
            larger_argv: Some(
                ["agrep", "--classic", "-n", "80", "--", "deploy"]
                    .map(String::from)
                    .to_vec(),
            ),
            ..Style::default()
        };
        assert_eq!(
            completeness_line(&r, &s, true),
            "showing 40 of 118+ matching rows (a floor, not the total; -c counts exactly) · \
             larger page: agrep --classic -n 80 -- deploy"
        );
        let s = Style {
            semantic: true,
            ..Style::default()
        };
        assert_eq!(
            completeness_line(&r, &s, false),
            "showing 40 of 118+ chats (a floor, not the total) · the meaning lane ranks a \
             bounded candidate set; no invocation returns every semantically related row"
        );
    }
}
