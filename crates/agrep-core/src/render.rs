//! Search output: stable machine rows and frozen, byte-bounded agent pages.
use crate::{
    boundary_rank,
    data::DataRoot,
    evidence::{content_terms, py_space},
    pages,
    pattern::Pattern,
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
    let n = (crate::clock::now_ms() - ts).max(0) / 1000;
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
        crate::errln!("{NO_AUTO_STALE}");
    }
}
/// The machine freshness record; under `--no-auto` it is 0.4.9's: nothing checked the
/// sources, so the index may be stale.
pub fn machine_freshness(no_auto: bool) -> Value {
    if no_auto {
        json!({"state":"degraded","failing":true,"may_be_stale":true,"code":"search-index-stale","reason":"automatic freshness checks are disabled by --no-auto","checked":false})
    } else {
        freshness(checked(no_auto))
    }
}
pub fn checked(no_auto: bool) -> bool {
    !no_auto && !crate::data::read_only()
}
pub fn freshness(checked: bool) -> Value {
    if checked {
        json!({"state":"no-known-failure","failing":false,"checked":true})
    } else {
        json!({"state":"unchecked","failing":false,"checked":false})
    }
}
/// A search hit's JSON row. A meaning row is 0.4.9's (py/search.py:1010-1038, py/ask.py:
/// 2613-2624): its cosine to six places, the keyword formula as `score`, no lane or match
/// label, and the empty concept and summary and unknown model source its message lane
/// reported.
pub fn hit_value(h: &Hit, now: i64) -> Value {
    let mut value = if h.matched == "semantic" {
        json!({"session":h.session,"agent":h.agent,"project":h.project,"concept":"","model":h.model,"model_source":"unknown","turn":h.turn,"ts":h.ts,"who":h.who,"sem_score":crate::window::round6(h.score),"score_kind":"cosine","snippet":h.snippet,"summary":"","semantic_source":"message","score":crate::query::meaning_score(h, now),"handle":h.handle,"self":h.self_marker})
    } else {
        let mut value = json!({"session":h.session,"agent":h.agent,"project":h.project,"model":h.model,"model_source":if h.who=="tool"{"tool"}else{&h.model_source},"turn":h.turn,"ts":h.ts,"who":h.who,"snippet":h.snippet,"score":h.score,"handle":h.handle,"self":h.self_marker});
        if h.matched != "phrase" {
            value["matched"] = json!(h.matched)
        }
        value
    };
    if !h.forks.is_empty() {
        value["forks"] = h
            .forks
            .iter()
            .map(|fork| json!({"session":fork.session,"handle":fork.handle}))
            .collect();
    }
    value
}
/// 0.4.9's snippet patterns: what re-finds a hit's match in its snippet, to paint it
/// (`_hl`) and to center the compact page's narrower window (`_compact_snippet`).
pub struct Marks {
    /// `_match_pat`: the phrase with `[\W_]*` between its tokens, a whole word, or the
    /// user's regex; none for a meaning-only result.
    pattern: Option<Pattern>,
    /// `_terms_hl_pat`: each content term, for keyword rows matched in any order.
    terms: std::sync::OnceLock<Option<Pattern>>,
    query: String,
    keyword: bool,
    /// The query's boundary terms, which place an any-order row's matches.
    prepared: std::sync::OnceLock<boundary_rank::PreparedQuery>,
}
impl Marks {
    pub fn new(q: &str, s: &Style) -> Self {
        let keyword = !s.semantic && !s.regex && !s.word;
        let pattern = if s.semantic {
            None
        } else if s.regex {
            Pattern::regex(q).ok()
        } else if s.word {
            (!q.is_empty()).then(|| Pattern::word(q).ok()).flatten()
        } else {
            let tokens: Vec<String> = q
                .split(|c: char| py_space(c) || c == '-' || c == '_')
                .filter(|t| !t.is_empty())
                .map(crate::pattern::literal)
                .collect();
            (!tokens.is_empty())
                .then(|| {
                    let gap = if tokens.len() > 1 {
                        crate::unicode_v16::keyword_gap()
                    } else {
                        ""
                    };
                    Pattern::linear(&tokens.join(gap)).ok()
                })
                .flatten()
        };
        Self {
            pattern,
            terms: std::sync::OnceLock::new(),
            query: q.to_owned(),
            keyword,
            prepared: std::sync::OnceLock::new(),
        }
    }
    fn terms(&self) -> Option<&Pattern> {
        self.terms
            .get_or_init(|| {
                if !self.keyword {
                    return None;
                }
                let mut variants: Vec<String> = content_terms(&self.query)
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
                (!variants.is_empty())
                    .then(|| {
                        Pattern::linear(
                            &variants
                                .iter()
                                .map(|v| crate::pattern::literal(v))
                                .collect::<Vec<_>>()
                                .join("|"),
                        )
                        .ok()
                    })
                    .flatten()
            })
            .as_ref()
    }
    /// `_hl` with `_row_hl_pat`: the sanitized snippet with each match painted.
    fn paint(&self, h: &Hit, color: bool) -> String {
        let snippet = safe(&h.snippet);
        let pattern = if ["all-terms", "content-terms"].contains(&h.matched.as_str()) {
            self.terms().or(self.pattern.as_ref())
        } else {
            self.pattern.as_ref()
        };
        let Some(p) = pattern.filter(|_| color) else {
            return snippet;
        };
        let (mut painted, mut last) = (String::with_capacity(snippet.len()), 0);
        let done = p.each(&snippet, |m| {
            painted.push_str(&snippet[last..m.start]);
            painted.push_str("\x1b[1;31m");
            painted.push_str(&snippet[m.start..m.end]);
            painted.push_str("\x1b[0m");
            last = m.end;
        });
        if done.is_err() {
            return snippet;
        }
        painted.push_str(&snippet[last..]);
        painted
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
                .get_or_init(|| {
                    boundary_rank::prepare_query(&self.query, &boundary_rank::Stats::new())
                })
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
/// How many other chats hold a row's message: `also in 2 forks`.
fn forks_note(h: &Hit) -> Option<String> {
    match h.forks.len() {
        0 => None,
        1 => Some("also in 1 fork".into()),
        n => Some(format!("also in {} forks", surface::number(n))),
    }
}
/// The matching rows a page's rows stand for, their fork copies included.
fn rows_shown(r: &Results) -> usize {
    r.hits.iter().map(Hit::rows).sum()
}
pub fn compact_line(h: &Hit, marks: &Marks) -> String {
    let lane = if h.matched == "semantic" {
        if h.sem_weak {
            "~semantic-weak"
        } else {
            "~semantic"
        }
    } else if h.matched != "phrase" {
        "~all"
    } else if ["partial", "interior"].contains(&h.boundary_class.as_str()) {
        "~substr"
    } else {
        ""
    };
    let quality = match (h.matched == "semantic", forks_note(h)) {
        (true, Some(note)) => format!("{lane} (meaning match, {note})"),
        (true, None) => format!("{lane} (meaning match)"),
        (false, Some(note)) if lane.is_empty() => format!("({note})"),
        (false, Some(note)) => format!("{lane} ({note})"),
        (false, None) => lane.into(),
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
        quality,
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
/// 0.4.9's forced coverage lane on stderr (py/search.py:6468-6532): what the top row
/// matched of the query's words, then one compact line per row.
pub fn coverage_block(hits: &[Hit], q: &str, s: &Style) -> Vec<String> {
    let Some(top) = hits.first() else {
        return vec!["coverage lane: no candidate rows".into()];
    };
    // The words as 0.4.9 split them (py/corpusdb.py:5165-5166, 5193-5202): at whitespace,
    // hyphens and underscores, lowercased, each found anywhere in the lowercased row.
    let mut words: Vec<String> = vec![];
    for word in q
        .trim()
        .split(|c: char| c.is_whitespace() || c == '-' || c == '_')
        .filter(|w| !w.is_empty())
        .map(str::to_lowercase)
    {
        if !words.contains(&word) {
            words.push(word);
        }
    }
    let text = top.text.to_lowercase();
    let (matched, missing): (Vec<&String>, Vec<&String>) =
        words.iter().partition(|w| text.contains(w.as_str()));
    let detail = if missing.is_empty() {
        format!("top row matched all {} terms", words.len())
    } else {
        let mut dropped = missing
            .iter()
            .take(6)
            .map(|w| w.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        if missing.len() > 6 {
            dropped.push_str(", …");
        }
        format!(
            "top row matched {}/{} terms - dropped: {}",
            matched.len(),
            words.len(),
            safe(&dropped)
        )
    };
    let marks = Marks::new(q, s);
    std::iter::once(format!("coverage lane (forced): {detail}"))
        .chain(hits.iter().map(|h| compact_line(h, &marks)))
        .collect()
}
pub fn emit(r: &Results, q: &str, f: &Filters, s: &Style) -> Result<()> {
    emit_semantic(r, q, f, s, None, &[])
}
/// `after` goes to stderr below the rows and a compact page's footer, above the line
/// saying a classic page was cut.
pub fn emit_semantic(
    r: &Results,
    q: &str,
    _f: &Filters,
    s: &Style,
    coverage: Option<&crate::semantic::Coverage>,
    after: &[String],
) -> Result<()> {
    let truncated = ((!s.count && !s.tiers) && rows_shown(r) < r.total) || r.total_basis != "exact";
    let exhaustible = !s.semantic && !r.hits.iter().any(|h| h.matched == "semantic");
    let marks = std::cell::LazyCell::new(|| Marks::new(q, s));
    let mut out = String::new();
    let mut footer = None;
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
        out = json_lines(r, q, s, coverage, truncated, crate::clock::now_ms())?;
    } else if s.chats && !s.compact {
        // 0.4.9's `_emit_chats`: one head line per matching chat (`grep -l`).
        for (i, h) in r.hits.iter().enumerate() {
            let n = s.session_hits.get(i).copied().unwrap_or(1);
            out += &chat_head(h, n, s.color);
        }
    } else if s.flat {
        out = flat_page(r, s.color, |hit| {
            if s.color {
                marks.paint(hit, true)
            } else {
                safe(&hit.snippet)
            }
        });
    } else if s.compact {
        (out, footer) = compact_page(r, q, _f, s, &marks, truncated, exhaustible)?;
    } else {
        out = classic_page(r, |hit| marks.paint(hit, true));
    }
    surface::write(&out)?;
    if let Some(line) = footer {
        crate::errln!("{line}");
    }
    for line in after {
        crate::errln!("{line}");
    }
    if let Some(legend) = speaker_legend(s, std::io::stderr().is_terminal()) {
        crate::errln!("{legend}");
    }
    if truncated && !s.json && !s.compact && !s.count && !s.tiers {
        crate::errln!("{}", completeness_line(r, s, exhaustible));
    }
    Ok(())
}
/// One tab-separated row per page row: session, agent, project, turn, speaker and
/// snippet. A row standing for its copies in forks ends its snippet with `(also in N
/// forks)`; JSON lists them.
fn flat_page(r: &Results, color: bool, snippet: impl Fn(&Hit) -> String) -> String {
    let mut out = String::new();
    for h in &r.hits {
        let forks = match forks_note(h) {
            Some(note) if color => format!("\x1b[2m ({note})\x1b[0m"),
            Some(note) => format!(" ({note})"),
            None => String::new(),
        };
        out += &format!(
            "{}\t{}\t{}\t{}\t{}\t{}{forks}\n",
            safe(&h.session),
            safe(&h.agent),
            safe(&project(&h.project)),
            h.turn,
            safe(&h.who),
            snippet(h)
        );
    }
    out
}
fn classic_page(r: &Results, paint: impl Fn(&Hit) -> String) -> String {
    let mut out = String::new();
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
                "  \x1b[33m{:>4}\x1b[0m \x1b[2m{mark}\x1b[0m {}\x1b[2m{}{}{}\x1b[0m\n",
                h.turn,
                paint(h),
                if h.self_marker { " ~self" } else { "" },
                if h.meta_marker { " ~meta" } else { "" },
                forks_note(h)
                    .map(|note| format!(" {note}"))
                    .unwrap_or_default()
            );
        }
    }
    out
}
/// A compact page's rows in 0.4.9's compact order ([`diversify`]), keyword rows of the
/// families meaning rows lead last, cut at `limit` (0: none). A compact search ranks at
/// least the 40 rows of the page without `-n` whatever `-n` asks, so `-n` below 40 cuts the
/// order of those same rows: such a page is the first rows of each larger one up to 40.
pub fn compact_order(r: &mut Results, limit: usize) {
    let mut led = std::mem::take(&mut r.hits);
    let folded = led.split_off(led.len() - r.hybrid_folded);
    let mut hits = demote_meta(
        diversify(demote_meta(led))
            .into_iter()
            .chain(folded)
            .collect(),
    );
    if limit > 0 && hits.len() > limit {
        hits.truncate(limit);
        // Merged rows the cut left out are more rows the search held.
        if let Some(more) = r.hybrid_more.as_mut() {
            *more = true;
        }
    }
    r.hybrid_folded = 0;
    r.hits = hits;
}
/// A compact page's rows, in [`compact_order`], frozen behind their continuation, and the
/// footer printed below them.
fn compact_page(
    r: &Results,
    q: &str,
    f: &Filters,
    s: &Style,
    marks: &Marks,
    truncated: bool,
    exhaustible: bool,
) -> Result<(String, Option<String>)> {
    // A page merged with meaning rows is cut only when a lane held more, whatever its floor
    // total says (py/search.py:8321-8325).
    let cut = r.hybrid_more.unwrap_or(truncated);
    let frozen = pages::Frozen {
        records: r
            .hits
            .iter()
            .map(|h| pages::Record {
                line: compact_line(h, marks),
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
        chat_scoped: f.chat.is_some() || !s.diversify,
        full_argv: s.full_argv.clone(),
        // 0.4.9 offered `-c` on neither a meaning page nor a --no-meta page that dropped
        // rows, which bounds only what it fetched (py/search.py:6108-6110).
        exhaustible: exhaustible && r.meta_dropped + r.meta_retained == 0,
        larger_argv: s.larger_argv.clone().filter(|_| cut),
    };
    let page = pages::select(frozen);
    let more = match &s.data_root {
        Some(root) => pages::save(root, &page.remainder)?,
        None => None,
    };
    let footer = (more.is_some() || cut || !page.remainder.records.is_empty())
        .then(|| pages::footer(&page.remainder, more.as_deref()));
    Ok((page.text, footer))
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
    let mut meta = json!({"kind":"agrep-meta","query":q,"engine":"sqlite","freshness":machine_freshness(s.no_auto),"filter_coverage":s.filter_coverage,"self_exclusion":s.self_exclusion,"completeness":{"shown":rows_shown(r),"total":r.total,"total_basis":r.total_basis,"unit":if s.chats || s.semantic{"chat"}else{"matching row"},"truncated":truncated,"tool_rows":r.tool_rows},"semantic_coverage":null});
    if s.filter_coverage.is_null() {
        meta["filter_coverage"] = json!({"checked":true,"empty_dimensions":[]})
    }
    if s.self_exclusion.is_null() {
        meta["self_exclusion"] = json!({"active":false,"reason":"machine-surface"})
    }
    if let Some(coverage) = coverage {
        meta["semantic_coverage"] = serde_json::to_value(coverage)?;
    }
    if let Some(coverage) = coverage.filter(|_| s.semantic) {
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
    // `-c` counts rows and refuses `-l`, so a chat listing's floor names no exact form.
    let basis = match (exact, exhaustible && !r.tools_excluded && !s.chats) {
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
        rows_shown(r),
        r.total,
        if exact { "" } else { "+" },
        if r.total == 1 { "" } else { "s" }
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    // 0.4.9's `_hl` / `_hl_regex` paint raw-text Python re.I matches.
    #[test]
    fn keyword_marks_use_the_same_raw_case_classes_as_retrieval() {
        for (query, snippet, expected) in [
            (
                "kit",
                "KİT kıt kit",
                "\x1b[1;31mKİT\x1b[0m \x1b[1;31mkıt\x1b[0m \x1b[1;31mkit\x1b[0m",
            ),
            (
                "cafe kit",
                "cafe\u{301}—kit",
                "\x1b[1;31mcafe\u{301}—kit\x1b[0m",
            ),
            ("office", "oﬃce office", "oﬃce \x1b[1;31moffice\x1b[0m"),
            ("...", "… ...", "… \x1b[1;31m...\x1b[0m"),
        ] {
            let marks = Marks::new(query, &Style::default());
            let hit = Hit {
                snippet: snippet.into(),
                matched: "phrase".into(),
                ..Hit::default()
            };
            assert_eq!(marks.paint(&hit, true), expected, "{query:?}");
        }
    }

    #[test]
    fn semantic_rows_name_the_meaning_lane() {
        let hit = Hit {
            matched: "semantic".into(),
            session: "semantic-session".into(),
            ..Hit::default()
        };
        assert!(compact_line(&hit, &Marks::new("", &Style::default())).contains("meaning match"));
    }
    /// A meaning row standing for its copies in forks says so on every page: one flat row
    /// counts them where the classic row does, so `-n` bounds the rows a pipe gets.
    #[test]
    fn a_folded_meaning_row_names_its_forks_on_one_row() {
        let fork = |session: &str, turn| crate::query::Fork {
            session: session.into(),
            project: "merger".into(),
            turn,
            handle: format!("@{session}:{turn}.0000"),
        };
        let hit = Hit {
            matched: "semantic".into(),
            session: "fork-b".into(),
            agent: "pi".into(),
            project: "merger".into(),
            turn: 4,
            who: "user".into(),
            snippet: "dont overfit the prompt".into(),
            forks: vec![fork("fork-a", 4), fork("root", 3)],
            ..Hit::default()
        };
        let line = compact_line(&hit, &Marks::new("", &Style::default()));
        assert!(
            line.contains("~semantic (meaning match, also in 2 forks) "),
            "{line}"
        );
        let one = Hit {
            forks: vec![fork("root", 3)],
            ..hit.clone()
        };
        let page = |hit: Hit| {
            let mut r = Results::default();
            r.hits = vec![hit];
            r
        };
        assert!(classic_page(&page(one.clone()), |h| h.snippet.clone()).contains(" also in 1 fork"));
        let alone = Hit {
            forks: Vec::new(),
            ..hit.clone()
        };
        assert_eq!(
            flat_page(&page(hit), false, |h| h.snippet.clone()),
            "fork-b\tpi\tmerger\t4\tuser\tdont overfit the prompt (also in 2 forks)\n"
        );
        assert_eq!(
            flat_page(&page(one), true, |h| h.snippet.clone()),
            "fork-b\tpi\tmerger\t4\tuser\tdont overfit the prompt\x1b[2m (also in 1 fork)\x1b[0m\n"
        );
        assert_eq!(
            flat_page(&page(alone), false, |h| h.snippet.clone()),
            "fork-b\tpi\tmerger\t4\tuser\tdont overfit the prompt\n"
        );
    }
    /// A keyword row standing for its fork copies says so beside its match quality, lists
    /// them in JSON, and the page counts the rows it stands for: nothing is cut.
    #[test]
    fn a_folded_keyword_row_names_its_forks_and_counts_them() {
        let fork = |session: &str| crate::query::Fork {
            session: session.into(),
            project: "merger".into(),
            turn: 4,
            handle: format!("@{session}:4.0000"),
        };
        let hit = Hit {
            matched: "phrase".into(),
            session: "fork-b".into(),
            agent: "pi".into(),
            project: "merger".into(),
            turn: 4,
            who: "user".into(),
            snippet: "dont overfit the prompt".into(),
            forks: vec![fork("fork-a"), fork("root")],
            ..Hit::default()
        };
        let marks = Marks::new("", &Style::default());
        let line = compact_line(&hit, &marks);
        assert!(line.contains(" (also in 2 forks) dont overfit"), "{line}");
        let scatter = Hit {
            matched: "all-terms".into(),
            forks: vec![fork("root")],
            ..hit.clone()
        };
        let line = compact_line(&scatter, &marks);
        assert!(
            line.contains(" ~all (also in 1 fork) dont overfit"),
            "{line}"
        );
        let value = hit_value(&hit, 0);
        assert_eq!(value["forks"][1]["handle"], "@root:4.0000");
        assert!(value.get("matched").is_none());

        let mut r = Results::default();
        r.hits = vec![hit];
        r.total = 3;
        r.total_basis = "exact".into();
        let json = json_lines(&r, "overfit", &Style::default(), None, false, 0).unwrap();
        let meta: Value = serde_json::from_str(json.lines().next().unwrap()).unwrap();
        assert_eq!(meta["completeness"]["shown"], 3);
        assert_eq!(rows_shown(&r), r.total);
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
            json!({"completeness":{"shown":4,"tool_rows":0,"total":4,"total_basis":"exact","truncated":false,"unit":"chat"},"engine":"semantic:hybrid","filter_coverage":{"checked":true,"empty_dimensions":[]},"freshness":{"checked":true,"failing":false,"state":"no-known-failure"},"kind":"agrep-meta","query":q,"self_exclusion":{"active":false,"reason":"machine-surface"},"semantic":{"complete":true,"coverage":full,"eligible":true,"fallback_recommended":false,"min_score":0.82,"reason":null,"score_kind":"cosine","state":"ready"},"semantic_coverage":full})
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
            json!({"complete":true,"coverage":full,"eligible":true,"fallback_recommended":false,"min_score":0.82,"reason":null,"score_kind":"cosine","state":"no-confident-match"})
        );
    }
    // 0.4.9's real compact page for "how do we rotate secret credentials" on the six-chat
    // fixture with its models installed: the strong meaning row, then the other chats'
    // bag-of-words rows, and no footer, since neither lane held more. Under `-n 3` it showed
    // two rows and `7+ matches (floor) · broader rerun (may repeat): agrep --deeper m.<TOKEN>`,
    // whose rerun PORTING.md drops; 0.5's `-n 3` shows the first three rows of that page. The
    // lanes are that fixture's: seven content rows (each reply naming the secret store, and
    // the vault chat's question) and the meaning rows, only the vault chat's reply strong.
    // 0.4.9 dropped the vault chat's question as covered by its meaning row; 0.5 freezes it
    // behind the page, which now says it holds more.
    #[test]
    fn hybrid_compact_page_and_footer_match_0_4_9() {
        let row = |chat: u8, who: &str, digest: &str, score: f64, matched: &str, weak: bool| {
            let session = format!("01960000-0000-7000-8000-00000000f00{chat}");
            Hit {
                handle: format!("@{session}:0.{digest}"),
                family: session.clone(),
                session,
                agent: "pi".into(),
                project: format!("/work/sec{chat}"),
                who: who.into(),
                score,
                matched: matched.into(),
                sem_weak: weak,
                ..Hit::default()
            }
        };
        let lanes = || {
            let mut keyword = Results::default();
            keyword.hits = [
                (2, "agent", "f529", 0.1752),
                (2, "user", "question", 0.1),
                (5, "agent", "689b", 0.082),
                (4, "agent", "d404", 0.0781),
                (3, "agent", "883d", 0.0743),
                (1, "agent", "7c63", 0.0673),
                (0, "agent", "cb01", 0.064),
            ]
            .into_iter()
            .map(|(chat, who, digest, score)| row(chat, who, digest, score, "content-terms", false))
            .collect();
            keyword.total = 7;
            keyword.total_basis = "exact".into();
            let mut meaning = Results::default();
            meaning.hits = vec![
                row(0, "agent", "cb01", 0.882, "semantic", true),
                row(2, "agent", "f529", 0.870, "semantic", false),
                row(5, "agent", "689b", 0.845, "semantic", true),
                row(3, "agent", "883d", 0.836, "semantic", true),
            ];
            meaning.total = 4;
            meaning.total_basis = "exact".into();
            crate::hybrid::merge(keyword, Some(meaning), 40, true)
        };
        let q = "how do we rotate secret credentials";
        let page = |requested: Option<usize>| {
            let mut r = lanes();
            compact_order(&mut r, requested.unwrap_or(40));
            let s = Style {
                compact: true,
                diversify: true,
                requested,
                ..Style::default()
            };
            let truncated = r.hits.len() < r.total || r.total_basis != "exact";
            let (text, footer) = compact_page(
                &r,
                q,
                &Filters::default(),
                &s,
                &Marks::new(q, &s),
                truncated,
                false,
            )
            .unwrap();
            let handles: Vec<String> = text
                .lines()
                .map(|l| {
                    l.split(' ').next().unwrap()["@01960000-0000-7000-8000-00000000".len()..]
                        .to_owned()
                })
                .collect();
            (handles, footer)
        };
        assert_eq!(
            page(None),
            (
                [
                    "f002:0.f529",
                    "f005:0.689b",
                    "f004:0.d404",
                    "f003:0.883d",
                    "f001:0.7c63",
                    "f000:0.cb01"
                ]
                .map(String::from)
                .to_vec(),
                Some("7+ matches (floor)".to_owned())
            )
        );
        assert_eq!(
            page(Some(3)),
            (
                ["f002:0.f529", "f005:0.689b", "f004:0.d404"]
                    .map(String::from)
                    .to_vec(),
                Some("7+ matches (floor)".to_owned())
            )
        );
    }
    // The owner's history: one meaning row of an orchestrator family whose six subagent
    // chats hold every keyword row. 0.4.9 dropped all six behind `6+ matches (floor)`; each
    // must stay on the page or behind its continuation, and `-n` must show them all.
    #[test]
    fn keyword_rows_a_meaning_row_covers_stay_reachable() {
        let row = |session: &str, family: &str, matched: &str, score: f64| Hit {
            handle: format!("@{session}:0.aaaa"),
            session: session.into(),
            family: family.into(),
            agent: "pi".into(),
            who: "subagent".into(),
            text: format!("{session} semantic model files"),
            snippet: format!("{session} semantic model files"),
            score,
            matched: matched.into(),
            ..Hit::default()
        };
        let keyword_rows: Vec<String> = (1..=6).map(|i| format!("sub{i}")).collect();
        let merged = || {
            let mut keyword = Results::default();
            keyword.hits = keyword_rows
                .iter()
                .map(|s| row(s, "orch", "phrase", 0.5))
                .collect();
            keyword.total = 6;
            keyword.total_basis = "exact".into();
            let mut meaning = Results::default();
            meaning.hits = vec![
                row("other", "other", "semantic", 0.9),
                row("sub7", "orch", "semantic", 0.88),
            ];
            meaning.total = 2;
            meaning.total_basis = "exact".into();
            let mut r = crate::hybrid::merge(keyword, Some(meaning), 40, true);
            compact_order(&mut r, 40);
            r
        };
        let t = tempfile::tempdir().unwrap();
        let root = crate::data::DataRoot(t.path().to_owned());
        let q = "where are the semantic model files downloaded from";
        let sessions = |text: &str| -> Vec<String> {
            text.lines()
                .map(|l| l[1..].split(':').next().unwrap().to_owned())
                .collect()
        };
        let style = |requested: Option<usize>| Style {
            compact: true,
            diversify: true,
            requested,
            data_root: Some(root.clone()),
            ..Style::default()
        };
        let r = merged();
        let s = style(None);
        let (text, footer) = compact_page(
            &r,
            q,
            &Filters::default(),
            &s,
            &Marks::new(q, &s),
            true,
            false,
        )
        .unwrap();
        let mut seen = sessions(&text);
        assert_eq!(seen[..2], ["other", "sub7"]);
        let footer = footer.unwrap();
        assert!(footer.starts_with("8+ matches (floor) · more: agrep --more m."));
        let mut handle = footer.rsplit(' ').next().map(str::to_owned);
        while let Some(h) = handle {
            let page = pages::select(pages::load(&root, &h).unwrap());
            seen.extend(sessions(&page.text));
            handle = pages::save(&root, &page.remainder).unwrap();
        }
        seen.sort();
        let mut all = keyword_rows.clone();
        all.extend(["other".into(), "sub7".into()]);
        all.sort();
        assert_eq!(seen, all);
        let s = style(Some(40));
        let (text, _) = compact_page(
            &r,
            q,
            &Filters::default(),
            &s,
            &Marks::new(q, &s),
            true,
            false,
        )
        .unwrap();
        assert_eq!(sessions(&text).len(), 8);
    }
    // The owner's history for "production deployment live money verification rollback
    // config": one keyword row, and a meaning lane led by weak neighbors, one of them in the
    // keyword row's family, ahead of strong rows. A page read the meaning lane only `-n`
    // rows deep, so the three rows a merge takes changed with `-n`: the page held 1, 1, 3
    // and 2 rows at -n 1/2/5/10, and the -n 5 page's meaning row of that family was gone at
    // -n 10. Both lanes are now read as deep as the page without `-n` reads them whatever
    // `-n` asks, so every page is the first rows of each larger one.
    #[test]
    fn a_larger_n_keeps_every_row_of_a_smaller_hybrid_page() {
        let row = |session: &str, family: &str, matched: &str, score: f64, weak: bool| Hit {
            handle: format!("@{session}:1.abcd"),
            session: session.into(),
            family: family.into(),
            agent: "pi".into(),
            who: "agent".into(),
            turn: 1,
            text: format!("{session} production deployment rollback"),
            score,
            matched: matched.into(),
            sem_weak: weak,
            ..Hit::default()
        };
        let lanes = |meaning_rows: usize| {
            let mut keyword = Results::default();
            keyword.hits = vec![row("kept", "f", "phrase", 0.4, false)];
            keyword.total = 1;
            keyword.total_basis = "exact".into();
            let mut meaning = Results::default();
            meaning.hits = [
                ("cousin", "f", 0.88, true),
                ("near", "g", 0.87, true),
                ("strong", "h", 0.86, false),
                ("near2", "i", 0.85, true),
                ("near3", "j", 0.849, true),
                ("strong2", "k", 0.848, false),
                ("strong3", "l", 0.847, false),
            ]
            .into_iter()
            .take(meaning_rows)
            .map(|(session, family, score, weak)| row(session, family, "semantic", score, weak))
            .collect();
            meaning.total = meaning.hits.len();
            meaning.total_basis = "exact".into();
            (keyword, meaning)
        };
        let sessions =
            |r: Results| -> Vec<String> { r.hits.into_iter().map(|h| h.session).collect() };
        let per_n = |n: usize| {
            let (keyword, meaning) = lanes(n);
            let mut r = crate::hybrid::merge(keyword, Some(meaning), n, true);
            compact_order(&mut r, n);
            sessions(r)
        };
        assert_eq!(per_n(5), ["strong", "cousin", "kept"]);
        assert_eq!(per_n(10), ["kept", "strong"]);
        let page = |n: usize| {
            let (keyword, meaning) = lanes(40);
            let mut r = crate::hybrid::merge(keyword, Some(meaning), 40, true);
            compact_order(&mut r, n);
            sessions(r)
        };
        let largest = page(20);
        assert_eq!(largest, ["kept", "strong"]);
        for n in 1..20 {
            assert_eq!(page(n), largest[..n.min(largest.len())], "-n {n}");
        }
    }
    // A compact page orders the rows the page without `-n` ranks (lived rows first, each
    // family's first row before its others), so a smaller `-n` cuts that order rather than
    // ordering fewer rows, which put a family's second row before another family's first.
    #[test]
    fn compact_rows_are_cut_from_the_full_page_order() {
        let row = |session: &str, family: &str, meta: bool| Hit {
            session: session.into(),
            family: family.into(),
            agent: "pi".into(),
            matched: "phrase".into(),
            meta_marker: meta,
            ..Hit::default()
        };
        let ranked = || {
            let mut r = Results::default();
            r.hits = vec![
                row("a1", "a", false),
                row("a2", "a", false),
                row("m1", "m", true),
                row("b1", "b", false),
                row("c1", "c", false),
            ];
            r
        };
        let page = |n: usize| {
            let mut r = ranked();
            compact_order(&mut r, n);
            r.hits.into_iter().map(|h| h.session).collect::<Vec<_>>()
        };
        assert_eq!(page(0), ["a1", "b1", "c1", "a2", "m1"]);
        for n in 1..=5 {
            assert_eq!(page(n), page(0)[..n], "-n {n}");
        }
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
