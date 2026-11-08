//! Around-window selection, disclosures and chronological output.
use super::{
    cap, one_line, selected_tool_match,
    tools::{tool_block, CYAN, DIM, HEAD, RESET, YELLOW},
    Event, Row, Window, MARKERS,
};
use crate::{query::content_digest, surface};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::collections::BTreeSet;

#[derive(Clone, Debug)]
pub struct Display {
    pub caller: crate::caller::Caller,
    pub json: bool,
    pub full: bool,
    pub max_chars: usize,
    pub explicit_max_chars: bool,
    pub no_tools: bool,
    pub tool_output: usize,
    pub who: Option<String>,
    pub color: bool,
}
/// An `around` result: the rendered page and whether it shows tool events, or 0.4.9's
/// `no-speaker-match` miss when the window holds nothing the selection keeps.
pub enum Rendered {
    Page { text: String, tools: bool },
    Miss { reason: String, scope: Value },
}
pub fn render(w: &Window, d: &Display) -> Result<Rendered> {
    if d.no_tools && w.claim.as_ref().is_some_and(|c| c.event.is_some()) {
        bail!("--no-tools conflicts with this tool result handle because it would erase the cited evidence")
    }
    let target = w.target.clone();
    let selected = select(w, d);
    if selected.rows.is_empty() && selected.events.is_empty() {
        return Ok(miss(w, d, &target));
    }
    let delegated_exact = w.side
        && w.claim
            .as_ref()
            .is_some_and(|c| c.digest.is_some() && c.event.is_none())
        && !d.explicit_max_chars
        && !d.full;
    let row_cap = if d.full || delegated_exact {
        0
    } else {
        d.max_chars
    };
    let (rendered, truncated) = render_rows(d, &target, &selected.rows, row_cap);
    let page = Page {
        w,
        d,
        target,
        center: w.center.to_string(),
        selected,
        rendered,
        truncated,
        row_cap,
        delegated_exact,
    };
    let mut out = page.header()?;
    let turns: BTreeSet<_> = page
        .selected
        .rows
        .iter()
        .map(|r| r.turn)
        .chain(page.selected.events.iter().map(|e| e.turn))
        .collect();
    for turn in turns {
        page.render_turn(&mut out, turn)?;
    }
    Ok(Rendered::Page {
        text: out,
        tools: !page.selected.events.is_empty(),
    })
}

struct Selection<'a> {
    rows: Vec<&'a Row>,
    events: Vec<&'a Event>,
    tool_mode: &'static str,
}

fn select<'a>(w: &'a Window, d: &Display) -> Selection<'a> {
    let selected = |r: &&Row| {
        d.who
            .as_ref()
            .map(|who| who.split(',').any(|s| s == r.who))
            .unwrap_or_else(|| {
                d.full
                    || ["user", "agent"].contains(&r.who.as_str())
                    || w.side
                    || w.claim.as_ref().is_some_and(|cl| {
                        cl.digest
                            .as_ref()
                            .is_some_and(|dg| r.turn == w.center && *dg == content_digest(&r.text))
                    })
            })
    };
    let rows: Vec<_> = w.rows.iter().filter(selected).collect();
    let tool_mode = if d.no_tools {
        "excluded"
    } else if d.full || d.tool_output > 0 || d.who.as_deref() == Some("tool") {
        "all"
    } else if w.claim.as_ref().is_some_and(|c| c.event.is_some()) {
        "selected"
    } else {
        "excluded"
    };
    let claim_event = w.claim.as_ref().and_then(|c| c.event.as_deref());
    let tools_wanted = d
        .who
        .as_deref()
        .is_none_or(|who| who.split(',').any(|s| s == "tool"));
    let events: Vec<&Event> = w
        .events
        .iter()
        .filter(|e| {
            tools_wanted
                && (tool_mode == "all"
                    || (tool_mode == "selected" && claim_event == Some(e.identity.as_str())))
        })
        .collect();
    Selection {
        rows,
        events,
        tool_mode,
    }
}

fn miss(w: &Window, d: &Display, target: &str) -> Rendered {
    let (first, last) = match (w.rows.first(), w.rows.last()) {
        (Some(first), Some(last)) => (first.turn, last.turn),
        _ => (w.center, w.center),
    };
    let role = d
        .who
        .as_deref()
        .map_or("default-scope".into(), |who| surface::safe(who, false));
    let mut reason = format!("no {role} messages or events in turns {first}-{last} of {target}");
    if d.who.is_none() {
        reason += "; use --full to include tool events";
    }
    let scope = json!({"session":w.session,"who":d.who,"first_turn":first,"last_turn":last});
    Rendered::Miss { reason, scope }
}

fn render_rows<'a>(
    d: &Display,
    target: &str,
    rows: &[&'a Row],
    row_cap: usize,
) -> (Vec<(&'a Row, String, usize)>, usize) {
    let mut rendered = Vec::new();
    let mut truncated = 0;
    for r in rows {
        let cmd = format!(
            "agrep around {target} {} -C 0 --max-chars 0{}",
            r.turn,
            if ["user", "agent"].contains(&r.who.as_str()) {
                String::new()
            } else {
                format!(" --who {}", r.who)
            }
        );
        let source = if !d.json && row_cap > 0 && r.who != "agent" {
            one_line(&r.text)
        } else {
            r.text.clone()
        };
        let (text, omitted) = cap(&source, row_cap, &cmd);
        truncated += usize::from(omitted > 0);
        rendered.push((*r, text, omitted));
    }
    (rendered, truncated)
}

struct EventCounts<'a> {
    markers_available: Vec<(&'a str, usize)>,
    markers_shown: Vec<(&'a str, usize)>,
    tools_available: usize,
    tools_shown: usize,
}

fn total(counts: &[(&str, usize)]) -> usize {
    counts.iter().map(|(_, n)| n).sum()
}

impl<'a> EventCounts<'a> {
    fn new(w: &'a Window, events: &[&'a Event]) -> Self {
        let all_events: Vec<&Event> = w.events.iter().collect();
        let markers_available = tally(
            all_events
                .iter()
                .filter(|e| e.is_marker())
                .map(|e| &*e.name),
        );
        let markers_shown = tally(events.iter().filter(|e| e.is_marker()).map(|e| &*e.name));
        let tools_available = all_events.len() - total(&markers_available);
        let tools_shown = events.len() - total(&markers_shown);
        Self {
            markers_available,
            markers_shown,
            tools_available,
            tools_shown,
        }
    }
}

struct Page<'a> {
    w: &'a Window,
    d: &'a Display,
    target: String,
    center: String,
    selected: Selection<'a>,
    rendered: Vec<(&'a Row, String, usize)>,
    truncated: usize,
    row_cap: usize,
    delegated_exact: bool,
}

impl Page<'_> {
    fn around(&self, extra: &[&str]) -> Vec<String> {
        let mut argv: Vec<String> = vec!["agrep".into(), "around".into(), self.target.clone()];
        argv.push(self.center.clone());
        match self.w.radius {
            Some(radius) => argv.extend(["-C".into(), radius.to_string()]),
            None => argv.push("--whole".into()),
        }
        argv.extend(extra.iter().map(|s| s.to_string()));
        argv
    }

    fn header(&self) -> Result<String> {
        let counts = EventCounts::new(self.w, &self.selected.events);
        if self.d.json {
            self.json_header(&counts)
        } else {
            Ok(self.text_header(&counts))
        }
    }

    fn json_header(&self, counts: &EventCounts<'_>) -> Result<String> {
        let (w, d, rows) = (self.w, self.d, &self.selected.rows);
        let (row_cap, delegated_exact, truncated) =
            (self.row_cap, self.delegated_exact, self.truncated);
        let tool_mode = self.selected.tool_mode;
        let claim_event = w.claim.as_ref().and_then(|c| c.event.as_deref());
        let (markers_available, markers_shown) = (&counts.markers_available, &counts.markers_shown);
        let (tools_available, tools_shown) = (counts.tools_available, counts.tools_shown);
        let mut out = String::new();
        let all: Vec<_> = w.rows.iter().collect();
        let prose_available = tally(all.iter().map(|r| &*r.who));
        let prose_shown = tally(rows.iter().map(|r| &*r.who));
        let mut meta = json!({"kind":"agrep-meta","scope":{"policy":if d.full{"forensic"}else if w.claim.is_none(){"root_prose"}else if claim_event.is_some(){"role_aware"}else{"bounded_inclusive"},"session":w.session,"selected_session_role":if w.side{"delegated"}else{"root_main"},"selected_record_role":if w.claim.is_none(){"none"}else if claim_event.is_some(){"tool"}else{"prose_turn_inclusive"},"center_turn":w.center,"radius":w.radius.unwrap_or((w.center-w.first).max(w.last-w.center)),"bounds":{"first_indexed_turn":w.first,"last_indexed_turn":w.last},"selection_order":if w.radius.is_none(){"whole_chat"}else if w.latest{"newest_tail"}else{"requested_center"},"render_order":"chronological","family_widened":false,"project_or_folder_ranked":false,"counts_state":"exact","tools":{"mode":tool_mode,"available":tools_available,"shown":tools_shown,"hidden":tools_available.saturating_sub(tools_shown)},"markers":{"available_by_name":object(markers_available),"shown_by_name":object(markers_shown),"hidden":total(markers_available).saturating_sub(total(markers_shown))},"prose":{"available_by_role":object(&prose_available),"shown_by_role":object(&prose_shown),"hidden":w.rows.len()-rows.len()},"adoption":if w.side{"unresolved"}else{"root_main"},"truncation":{"message_cap_chars":if row_cap==0{None}else{Some(row_cap)},"message_cap_state":if d.full{"forensic_uncapped"}else if delegated_exact{"selected_delegated_exact_turn_uncapped"}else if row_cap==0{"explicit_uncapped"}else{"bounded"},"render_truncated_rows":truncated,"source_truncated_agent_rows":w.rows.iter().filter(|r|r.reply_truncated()).count(),"source_truncated_events":w.events.iter().filter(|e|e.input_truncated()||e.output_truncated()).count()},"expansion_argv":self.around(&["--full"])}});
        meta["self_exclusion"] = d.caller.metadata(
            crate::caller::Request {
                waived: Some("chat-filter"),
                ..Default::default()
            },
            None,
        );
        if !w.notes.is_empty() {
            meta["served"] = json!({"served_as_requested":false,"divergences":w.notes});
        }
        out += &format!("{}\n", crate::json_output::to_string(&meta)?);
        Ok(out)
    }

    fn text_header(&self, counts: &EventCounts<'_>) -> String {
        let (w, d, rows, target) = (self.w, self.d, &self.selected.rows, &self.target);
        let (markers_available, markers_shown) = (&counts.markers_available, &counts.markers_shown);
        let (tools_available, tools_shown) = (counts.tools_available, counts.tools_shown);
        let mut out = String::new();
        let head = [target.as_str(), &w.agent, &w.project]
            .into_iter()
            .filter(|s| !s.is_empty())
            .map(|s| surface::safe(s, false))
            .collect::<Vec<_>>()
            .join(" · ");
        let lo = w.rows.first().map(|r| r.turn).unwrap_or(w.center);
        let hi = w.rows.last().map(|r| r.turn).unwrap_or(w.center);
        let span = format!("turns {lo}-{hi} of {}-{}", w.first, w.last);
        out += &if d.color {
            format!("{HEAD}{head}{RESET}  {DIM}{span}{RESET}\n")
        } else {
            format!("{head}  {span}\n")
        };
        let hidden_tools = tools_available.saturating_sub(tools_shown);
        let hidden_prose = w.rows.len() - rows.len();
        let count = |counts: &[(&str, usize)], name: &str| {
            counts
                .iter()
                .find(|(n, _)| *n == name)
                .map_or(0, |(_, n)| *n)
        };
        let hidden_markers: Vec<String> = MARKERS
            .iter()
            .filter_map(|(name, noun)| {
                let n = count(markers_available, name) - count(markers_shown, name);
                (n > 0).then(|| {
                    format!(
                        "{} {noun}{}",
                        surface::number(n),
                        if n == 1 { "" } else { "s" }
                    )
                })
            })
            .collect();
        let hidden = hidden_tools > 0 || hidden_prose > 0 || !hidden_markers.is_empty();
        let mut parts = Vec::new();
        if w.side {
            parts.push("subagent conversation".to_owned());
        }
        if hidden_tools > 0 {
            parts.push(format!(
                "{} tool events omitted",
                surface::number(hidden_tools)
            ));
        }
        if !hidden_markers.is_empty() {
            parts.push(format!("{} omitted", hidden_markers.join(", ")));
        }
        if hidden_prose > 0 {
            parts.push(format!(
                "{} messages omitted",
                surface::number(hidden_prose)
            ));
        }
        if hidden {
            // A speaker-filtered read is lifted by dropping the filter, not by --full.
            let extra: &[&str] = if d.who.is_some() { &[] } else { &["--full"] };
            parts.push(surface::shell(&self.around(extra)));
        }
        if !parts.is_empty() {
            let line = parts.join(" · ");
            out += &if d.color {
                format!("{DIM}{line}{RESET}\n")
            } else {
                format!("{line}\n")
            };
        }
        out
    }

    fn render_turn(&self, out: &mut String, turn: i64) -> Result<()> {
        let (w, d, target, rendered, events) = (
            self.w,
            self.d,
            &self.target,
            &self.rendered,
            &self.selected.events,
        );
        let claim = (
            w.claim.as_ref().and_then(|c| c.event.as_deref()),
            w.claim.as_ref().and_then(|c| c.span),
        );
        if !d.json {
            let ts = w.rows.iter().find(|r| r.turn == turn).map_or(0, |r| r.ts);
            let (rule, stamp) = ("─".repeat(40), surface::timestamp(ts));
            *out += &if d.color {
                format!("{DIM}── turn {RESET}{YELLOW}{turn}{RESET} {DIM}{rule} {stamp}{RESET}\n")
            } else {
                format!("── turn {turn} {rule} {stamp}\n")
            };
        }
        for (r, text, omitted) in rendered
            .iter()
            .filter(|(r, _, _)| r.turn == turn && (d.json || r.who != "agent"))
        {
            *out += &prose_output(w, d, r, text, *omitted);
        }
        let turn_events: Vec<&Event> = events.iter().copied().filter(|e| e.turn == turn).collect();
        if d.json {
            for e in turn_events {
                let m = selected_tool_match(e, claim.0, claim.1);
                let mut v = json!({"kind":e.kind,"session":w.session,"project":w.project,"turn":e.turn,"ts":e.ts,"name":e.name,"input":e.input,"ok":e.ok,"input_chars":e.input_chars,"output_chars":e.output_chars,"output_bytes":e.output_bytes,"input_truncated":e.input_truncated(),"output_truncated":e.output_truncated()});
                if let Some(preview) = m.preview.filter(|p| m.selected && !p.is_empty()) {
                    v["match_preview"] = json!(preview);
                }
                if d.tool_output > 0 {
                    v["output"] = json!(e.output.chars().take(d.tool_output).collect::<String>())
                }
                *out += &format!("{}\n", crate::json_output::to_string(&v)?);
            }
        } else {
            let expand = surface::shell(&[
                "agrep".into(),
                "around".into(),
                target.clone(),
                turn.to_string(),
                "-C".into(),
                "0".into(),
                "--full".into(),
            ]);
            for line in tool_block(
                &turn_events,
                d.color,
                d.tool_output,
                &expand,
                !d.full,
                claim,
            ) {
                *out += &line;
                out.push('\n');
            }
        }
        for (r, text, omitted) in rendered
            .iter()
            .filter(|(r, _, _)| r.turn == turn && !d.json && r.who == "agent")
        {
            *out += &prose_output(w, d, r, text, *omitted);
        }
        Ok(())
    }
}

/// Counts by name in first-seen order, as 0.4.9's dicts kept them.
fn tally<'a>(names: impl Iterator<Item = &'a str>) -> Vec<(&'a str, usize)> {
    let mut counts: Vec<(&str, usize)> = Vec::new();
    for name in names {
        match counts.iter_mut().find(|(n, _)| *n == name) {
            Some((_, count)) => *count += 1,
            None => counts.push((name, 1)),
        }
    }
    counts
}
fn object(counts: &[(&str, usize)]) -> Value {
    Value::Object(
        counts
            .iter()
            .map(|(name, count)| ((*name).to_owned(), json!(count)))
            .collect(),
    )
}
/// 0.4.9's `_dim_markers`: cap and ingest-loss markers in a body read as plumbing.
fn dim_markers(text: &str, color: bool) -> String {
    static MARKER: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"\[(?:\+[\d,]+ chars - [^\]]*|source[^\]]*at ingest[^\]]*)\]")
            .expect("valid marker pattern")
    });
    if !color {
        return text.to_owned();
    }
    MARKER
        .replace_all(text, |c: &regex::Captures| format!("{DIM}{}{RESET}", &c[0]))
        .into_owned()
}
fn prose_output(w: &Window, d: &Display, r: &Row, text: &str, omitted: usize) -> String {
    if d.json {
        let mut v = json!({"kind":"msg","self":d.caller.resolved && d.caller.agent == w.agent && d.caller.session == w.session,"session":w.session,"project":w.project,"turn":r.turn,"who":r.who,"ts":r.ts,"text":text,"omitted_chars":omitted});
        if r.who == "agent" {
            v["reply_chars"] = json!(r.source_chars);
            v["reply_truncated"] = json!(r.reply_truncated())
        }
        return format!(
            "{}\n",
            crate::json_output::to_string(&v).expect("serializing JSON value")
        );
    }
    let loss = if r.reply_truncated() {
        format!(
            " [source truncated at ingest; original reply {} chars]",
            surface::number(r.source_chars)
        )
    } else {
        String::new()
    };
    let text = dim_markers(&(surface::safe(text, true) + &loss), d.color);
    let who = surface::safe(&r.who, false);
    if d.color {
        let tone = if r.who == "agent" { CYAN } else { YELLOW };
        format!("{tone}{who}:{RESET} {text}\n")
    } else {
        format!("{who}: {text}\n")
    }
}
