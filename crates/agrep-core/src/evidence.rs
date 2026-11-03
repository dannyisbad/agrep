//! Canonical transcript windows, read together with events in one SQLite snapshot.
use crate::{
    handles::{self, Claim},
    query::content_digest,
    store, surface,
};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Row {
    pub session: String,
    pub turn: i64,
    pub who: String,
    pub ts: i64,
    pub text: String,
    pub model: String,
    pub model_source: String,
    pub source: i64,
    pub source_chars: usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub turn: i64,
    pub ts: i64,
    pub name: String,
    pub kind: String,
    pub input: String,
    pub output: String,
    pub input_chars: usize,
    pub output_chars: usize,
    pub output_bytes: Option<usize>,
    pub ok: Option<bool>,
    pub identity: String,
    pub text: String,
    pub raw: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Window {
    #[serde(skip)]
    pub target: String,
    pub session: String,
    pub agent: String,
    pub project: String,
    pub title: String,
    pub family: String,
    pub side: bool,
    pub center: i64,
    pub radius: Option<i64>,
    pub latest: bool,
    pub first: i64,
    pub last: i64,
    pub rows: Vec<Row>,
    pub events: Vec<Event>,
    pub notes: Vec<Value>,
    pub claim: Option<Claim>,
}
#[derive(Clone, Debug)]
pub struct Request {
    pub target: String,
    pub turn: Option<i64>,
    pub radius: Option<i64>,
    pub exact: bool,
}
impl Request {
    pub fn new(target: &str, turn: Option<i64>, radius: Option<i64>) -> Self {
        Self {
            target: target.into(),
            turn,
            radius,
            exact: handles::is_handle(target) || (target.starts_with('@') && target.contains(':')),
        }
    }
}
pub fn load(c: &Connection, request: &Request) -> Result<Window> {
    let claim = if request.exact {
        Some(handles::parse(&request.target)?)
    } else {
        None
    };
    if claim.is_some() && request.turn.is_some() {
        bail!("a result handle already includes its turn")
    }
    let (target, requested) = if let Some(claim) = &claim {
        (claim.session.clone(), Some(claim.turn))
    } else if request.turn.is_none() {
        match request.target.rsplit_once(':') {
            Some((s, t)) if t.parse::<i64>().is_ok() => (s.to_owned(), Some(t.parse()?)),
            _ => (request.target.clone(), None),
        }
    } else {
        (request.target.clone(), request.turn)
    };
    let candidates = handles::candidates(c, &target)?;
    if candidates.is_empty() {
        bail!("no indexed session matches {target}; rerun the search for a current handle")
    }
    let session = if candidates.len() == 1 {
        candidates[0].clone()
    } else if let Some(ref claim) = claim {
        let holders: Vec<_> = candidates
            .iter()
            .filter(|s| {
                claim.digest.as_ref().is_some_and(|d| {
                    has_claim(c, s, claim.turn, d, claim.event.as_deref()).unwrap_or(false)
                })
            })
            .cloned()
            .collect();
        if holders.len() != 1 {
            bail!(
                "result handle session is ambiguous: {}",
                candidates.join(", ")
            )
        }
        holders[0].clone()
    } else {
        bail!("session is ambiguous: {}", candidates.join(", "))
    };
    let (first, last): (Option<i64>, Option<i64>) = c.query_row(
        "SELECT min(position),max(position) FROM prose WHERE session=? AND visible=1",
        [&session],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let first = first.context("session has no indexed turns")?;
    let last = last.unwrap_or(first);
    let requested = requested.unwrap_or(last);
    let mut center = requested.clamp(first, last);
    let mut notes = Vec::new();
    if center != requested {
        if request.exact {
            bail!("result handle is stale: turn {requested} is outside chat turns {first}-{last}; rerun the search for a current handle")
        }
        notes.push(json!({"kind":"turn_clamped","requested":requested,"served":center,"note":format!("turn {requested} clamped to {center} (chat has turns {first}-{last})")}));
    }
    if let Some(claim) = &claim {
        if let Some(digest) = &claim.digest {
            if !has_claim(c, &session, center, digest, claim.event.as_deref())? {
                let radius = request.radius.unwrap_or(last - first).max(4);
                let positions:Vec<i64>=c.prepare("SELECT DISTINCT position FROM prose WHERE session=? AND visible=1 AND position BETWEEN ? AND ? ORDER BY position")?.query_map(params![session,center.saturating_sub(radius),center.saturating_add(radius)],|r|r.get(0))?.collect::<rusqlite::Result<_>>()?;
                let mut holders = Vec::new();
                for p in positions {
                    if has_claim(c, &session, p, digest, claim.event.as_deref())? {
                        holders.push(p)
                    }
                }
                match holders.len(){0=>bail!("result handle no longer names the cited content; rerun the search for a current handle"),1=>{center=holders[0];notes.push(json!({"kind":"content_moved","requested":requested,"served":center,"note":format!("handle content moved: turn {requested} → {center} (session was renumbered)")}));},_=>bail!("handle content is ambiguous in session {}: turns {}; rerun the search for a current handle",handles::target(&session,&[]),holders.iter().map(i64::to_string).collect::<Vec<_>>().join(", "))}
            }
        }
    }
    let (lo, hi) = request
        .radius
        .map(|r| (center.saturating_sub(r), center.saturating_add(r)))
        .unwrap_or((first, last));
    let (agent, project, title, family, side) = c.query_row(
        "SELECT agent,project,title,family,side FROM sessions WHERE id=?",
        [&session],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
    )?;
    let rows = load_rows(c, &session, lo, hi)?;
    let events = load_events(c, &session, lo, hi)?;
    Ok(Window {
        target: handles::target(&session, &handles::sessions(c)?),
        session,
        agent,
        project,
        title,
        family,
        side,
        center,
        radius: request.radius,
        latest: requested == last && request.turn.is_none() && claim.is_none(),
        first,
        last,
        rows,
        events,
        notes,
        claim,
    })
}
fn has_claim(
    c: &Connection,
    session: &str,
    turn: i64,
    digest: &str,
    event: Option<&str>,
) -> Result<bool> {
    for table in ["prose", "tools"] {
        if event.is_some() && table == "prose" {
            continue;
        }
        let mut q = c.prepare(&format!(
            "SELECT text,ts FROM {table} WHERE session=? AND position=? AND visible=1"
        ))?;
        for row in q.query_map(params![session, turn], |r| {
            Ok((r.get::<_, Vec<u8>>(0)?, r.get::<_, i64>(1)?))
        })? {
            let (raw, ts) = row?;
            let text = store::decode(table, raw)?;
            if content_digest(&text) == digest
                && event.is_none_or(|id| handles::event_id(session, ts, &text) == id)
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}
pub fn load_rows(c: &Connection, session: &str, lo: i64, hi: i64) -> Result<Vec<Row>> {
    let mut q=c.prepare("SELECT p.session,p.position,p.who,p.ts,p.text,p.model,p.model_source,p.source,s.body FROM prose p LEFT JOIN reply_shapes s ON s.source=p.source AND s.session=p.session AND s.turn=p.turn WHERE p.visible=1 AND p.session=? AND p.position BETWEEN ? AND ? ORDER BY p.position,CASE p.who WHEN 'agent' THEN 1 ELSE 0 END,p.ts,p.id")?;
    let mut rows = Vec::new();
    for row in q.query_map(params![session, lo, hi], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, i64>(3)?,
            r.get::<_, Vec<u8>>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
            r.get::<_, i64>(7)?,
            r.get::<_, Option<String>>(8)?,
        ))
    })? {
        let (session, turn, who, ts, bytes, model, model_source, source, shape) = row?;
        let text = store::decode("prose", bytes)?;
        let source_chars = if who == "agent" {
            shape
                .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                .and_then(|v| v["original_reply_chars"].as_u64())
                .map(|v| v as usize)
                .unwrap_or_else(|| text.chars().count())
        } else {
            text.chars().count()
        };
        rows.push(Row {
            session,
            turn,
            who,
            ts,
            text,
            model,
            model_source,
            source,
            source_chars,
        });
    }
    Ok(rows)
}
pub fn load_events(c: &Connection, session: &str, lo: i64, hi: i64) -> Result<Vec<Event>> {
    let mut q=c.prepare("SELECT p.position,p.ts,p.text,e.body FROM tools p LEFT JOIN events e ON e.source=p.source AND e.session=p.session AND e.call_id=p.extra WHERE p.visible=1 AND p.session=? AND p.position BETWEEN ? AND ? ORDER BY p.position,p.ts,p.id")?;
    let mut events = Vec::new();
    let mut seen = BTreeSet::new();
    for r in q.query_map(params![session, lo, hi], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, Vec<u8>>(2)?,
            r.get::<_, Option<String>>(3)?,
        ))
    })? {
        let (turn, ts, bytes, body) = r?;
        let text = store::decode("tools", bytes)?;
        let raw = body
            .map(|b| serde_json::from_str::<Value>(&b))
            .transpose()?
            .unwrap_or_else(|| json!({}));
        let string = |key: &str| raw[key].as_str().unwrap_or_default().to_owned();
        let identity = handles::event_id(session, ts, &text);
        if !seen.insert(identity.clone()) {
            continue;
        }
        let (head, tail) = text.split_once('\n').unwrap_or((&text, ""));
        let prefix = format!(
            "{}{}: ",
            string("name"),
            if raw["ok"] == false { " [failed]" } else { "" }
        );
        let input = if raw["input"].is_string() {
            string("input")
        } else {
            head.strip_prefix(&prefix).unwrap_or(head).to_owned()
        };
        let output = if raw["output"].is_string() {
            string("output")
        } else {
            tail.to_owned()
        };
        events.push(Event {
            turn,
            ts,
            name: string("name"),
            kind: raw["kind"].as_str().unwrap_or("tool").into(),
            input_chars: raw["input_chars"]
                .as_u64()
                .unwrap_or(input.chars().count() as u64) as usize,
            output_chars: raw["output_chars"]
                .as_u64()
                .unwrap_or(output.chars().count() as u64) as usize,
            output_bytes: raw["output_bytes"].as_u64().map(|v| v as usize),
            ok: raw["ok"].as_bool(),
            input,
            output,
            identity,
            text,
            raw,
        });
    }
    Ok(events)
}
/// Batch all windows on the same connection/snapshot; no source file is reopened.
pub fn windows(c: &Connection, requests: &[Request]) -> Result<Vec<Window>> {
    requests.iter().map(|r| load(c, r)).collect()
}
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
    pub checked: bool,
}
pub fn cap(text: &str, n: usize, command: &str) -> (String, usize) {
    let count = text.chars().count();
    if n == 0 || count <= n {
        return (text.into(), 0);
    }
    let prefix: String = text.chars().take(n).collect();
    let cut = prefix
        .rfind(' ')
        .filter(|i| prefix[*i..].chars().count() < 200)
        .unwrap_or(prefix.len());
    let kept = prefix[..cut].chars().count();
    (
        format!(
            "{} [+{} chars - {command}]",
            &prefix[..cut],
            surface::number(count - kept)
        ),
        count - kept,
    )
}
pub fn render(w: &Window, d: &Display) -> Result<String> {
    if d.no_tools && w.claim.as_ref().is_some_and(|c| c.event.is_some()) {
        bail!("--no-tools conflicts with this tool result handle because it would erase the cited evidence")
    }
    let target = w.target.clone();
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
    let events: Vec<_> = w
        .events
        .iter()
        .filter(|e| {
            tool_mode == "all"
                || (tool_mode == "selected"
                    && w.claim
                        .as_ref()
                        .and_then(|c| c.event.as_ref())
                        .is_some_and(|id| id == &e.identity))
        })
        .collect();
    let row_cap = if d.full
        || (w.side
            && w.claim
                .as_ref()
                .is_some_and(|c| c.digest.is_some() && c.event.is_none())
            && !d.explicit_max_chars)
    {
        0
    } else {
        d.max_chars
    };
    let mut rendered = Vec::new();
    let mut truncated = 0;
    for r in &rows {
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
            r.text.split_whitespace().collect::<Vec<_>>().join(" ")
        } else {
            r.text.clone()
        };
        let (text, omitted) = cap(&source, row_cap, &cmd);
        truncated += usize::from(omitted > 0);
        rendered.push((*r, text, omitted));
    }
    let expansion = vec![
        "agrep".to_owned(),
        "around".into(),
        target.clone(),
        w.center.to_string(),
        "-C".into(),
        w.radius
            .unwrap_or((w.center - w.first).max(w.last - w.center))
            .to_string(),
        "--full".into(),
    ];
    let mut out = String::new();
    if d.json {
        let counts = |rs: &[&Row]| {
            let mut v = BTreeMap::<String, usize>::new();
            for r in rs {
                *v.entry(r.who.clone()).or_default() += 1;
            }
            v
        };
        let all: Vec<_> = w.rows.iter().collect();
        let mut meta = json!({"kind":"agrep-meta","scope":{"policy":if d.full{"forensic"}else if w.claim.is_some(){"bounded_inclusive"}else{"root_prose"},"session":w.session,"selected_session_role":if w.side{"delegated"}else{"root_main"},"selected_record_role":if w.claim.is_none(){"none"}else if w.claim.as_ref().is_some_and(|c|c.event.is_some()){"tool"}else{"prose_turn_inclusive"},"center_turn":w.center,"radius":w.radius.unwrap_or((w.center-w.first).max(w.last-w.center)),"bounds":{"first_indexed_turn":w.first,"last_indexed_turn":w.last},"selection_order":if w.radius.is_none(){"whole_chat"}else if w.latest{"newest_tail"}else{"requested_center"},"render_order":"chronological","family_widened":false,"project_or_folder_ranked":false,"counts_state":"exact","tools":{"mode":tool_mode,"available":w.events.len(),"shown":events.len(),"hidden":w.events.len()-events.len()},"prose":{"available_by_role":counts(&all),"shown_by_role":counts(&rows),"hidden":w.rows.len()-rows.len()},"adoption":if w.side{"unresolved"}else{"root_main"},"truncation":{"message_cap_chars":if row_cap==0{None}else{Some(row_cap)},"message_cap_state":if d.full{"forensic_uncapped"}else if row_cap==0{"explicit_uncapped"}else{"bounded"},"render_truncated_rows":truncated,"source_truncated_agent_rows":rows.iter().filter(|r|r.source_chars>r.text.chars().count()).count(),"source_truncated_events":events.iter().filter(|e|e.input_chars>e.input.chars().count()||e.output_chars>e.output.chars().count()).count()},"expansion_argv":expansion}});
        meta["self_exclusion"] = d.caller.metadata(true, false);
        meta["self_exclusion"]["reason"] = json!("chat-filter");
        if !d.checked {
            meta["freshness"] = crate::render::freshness(false, 0);
        }
        if !w.notes.is_empty() {
            meta["served"] = json!({"served_as_requested":false,"divergences":w.notes});
        }
        out += &format!("{}\n", crate::json_output::to_string(&meta)?);
    } else {
        let head = [target.as_str(), &w.agent, &w.project]
            .into_iter()
            .filter(|s| !s.is_empty())
            .map(|s| surface::safe(s, false))
            .collect::<Vec<_>>()
            .join(" · ");
        let lo = w.rows.first().map(|r| r.turn).unwrap_or(w.center);
        let hi = w.rows.last().map(|r| r.turn).unwrap_or(w.center);
        out += &format!("{head}  turns {lo}-{hi} of {}-{}\n", w.first, w.last);
        if w.events.len() > events.len() || w.rows.len() > rows.len() || w.side {
            let mut parts = Vec::new();
            if w.side {
                parts.push("subagent conversation".into())
            }
            if w.events.len() > events.len() {
                parts.push(format!(
                    "{} tool events omitted",
                    w.events.len() - events.len()
                ))
            }
            if w.rows.len() > rows.len() {
                parts.push(format!("{} messages omitted", w.rows.len() - rows.len()))
            }
            out += &format!("{} · {}\n", parts.join(" · "), surface::shell(&expansion));
        }
    }
    let turns: BTreeSet<_> = rows
        .iter()
        .map(|r| r.turn)
        .chain(events.iter().map(|e| e.turn))
        .collect();
    for turn in turns {
        if !d.json {
            let ts = rows
                .iter()
                .find(|r| r.turn == turn)
                .map(|r| r.ts)
                .unwrap_or(0);
            out += &format!(
                "── turn {turn} {} {}\n",
                "─".repeat(40),
                surface::timestamp(ts)
            );
        }
        for (r, text, omitted) in rendered
            .iter()
            .filter(|(r, _, _)| r.turn == turn && (d.json || r.who != "agent"))
        {
            out += &prose_output(w, d, r, text, *omitted);
        }
        for e in events.iter().filter(|e| e.turn == turn) {
            let selected = w
                .claim
                .as_ref()
                .and_then(|c| c.event.as_ref())
                .is_some_and(|id| id == &e.identity);
            let preview = if selected {
                w.claim
                    .as_ref()
                    .and_then(|c| c.span)
                    .filter(|(_, end)| *end <= e.text.chars().count())
                    .map(|(start, end)| {
                        e.text
                            .chars()
                            .skip(start.saturating_sub(60))
                            .take((end - start + 120).min(360))
                            .collect::<String>()
                    })
            } else {
                None
            };
            if d.json {
                let mut v = json!({"kind":e.kind,"session":w.session,"project":w.project,"turn":e.turn,"ts":e.ts,"name":e.name,"input":e.input,"ok":e.ok,"input_chars":e.input_chars,"output_chars":e.output_chars,"output_bytes":e.output_bytes,"input_truncated":e.input_chars>e.input.chars().count(),"output_truncated":e.output_chars>e.output.chars().count()});
                if let Some(p) = preview {
                    v["match_preview"] = json!(p);
                }
                if d.tool_output > 0 {
                    v["output"] = json!(e.output.chars().take(d.tool_output).collect::<String>())
                }
                out += &format!("{}\n", crate::json_output::to_string(&v)?);
            } else {
                let input = surface::clip(
                    &surface::safe(preview.as_deref().unwrap_or(&e.input), false),
                    240,
                );
                let output = if d.tool_output == 0 && preview.is_none() && !e.output.is_empty() {
                    format!(" {}", surface::clip(&surface::safe(&e.output, false), 240))
                } else {
                    String::new()
                };
                let size = e
                    .output_bytes
                    .map(|n| format!(" ({}B)", surface::number(n)))
                    .unwrap_or_default();
                out += &format!(
                    "  {} {} {input}{output}{size}\n",
                    if e.ok == Some(false) {
                        "✗ FAILED"
                    } else {
                        "⚙"
                    },
                    surface::safe(&e.name, false)
                );
                if d.tool_output > 0 {
                    out += &format!(
                        "    {}{}\n",
                        surface::safe(
                            &e.output.chars().take(d.tool_output).collect::<String>(),
                            true
                        )
                        .replace('\n', "\n    "),
                        if e.output_chars > e.output.chars().count() {
                            " [source truncated at ingest]"
                        } else {
                            ""
                        }
                    )
                }
            }
        }
        for (r, text, omitted) in rendered
            .iter()
            .filter(|(r, _, _)| r.turn == turn && !d.json && r.who == "agent")
        {
            out += &prose_output(w, d, r, text, *omitted);
        }
    }
    Ok(out)
}

fn prose_output(w: &Window, d: &Display, r: &Row, text: &str, omitted: usize) -> String {
    if d.json {
        let mut v = json!({"kind":"msg","self":d.caller.resolved && d.caller.agent == w.agent && d.caller.session == w.session,"session":w.session,"project":w.project,"turn":r.turn,"who":r.who,"ts":r.ts,"text":text,"omitted_chars":omitted});
        if r.who == "agent" {
            v["reply_chars"] = json!(r.source_chars);
            v["reply_truncated"] = json!(r.source_chars > r.text.chars().count())
        }
        return format!(
            "{}\n",
            crate::json_output::to_string(&v).expect("serializing JSON value")
        );
    }
    let loss = if r.source_chars > r.text.chars().count() {
        format!(
            " [source truncated at ingest; original reply {} chars]",
            surface::number(r.source_chars)
        )
    } else {
        String::new()
    };
    let text = surface::safe(text, true);
    if d.color {
        format!(
            "{}{}:\x1b[0m {text}{loss}\n",
            if r.who == "agent" {
                "\x1b[36m"
            } else {
                "\x1b[33m"
            },
            r.who
        )
    } else {
        format!("{}: {text}{loss}\n", r.who)
    }
}
