//! Classify the final turn and assemble pending briefing records.
use super::{
    prose::{blocks, direct_request, final_sentence, one_line, open_section, prose, re},
    todos::todos,
    Event,
};
use crate::{
    history::{self, Row, Session},
    model::ReplyShape,
};
use anyhow::Result;
use rusqlite::{params, Connection};
use serde_json::{json, Value};

pub fn is_turn(rows: &[Row], row: &Row) -> bool {
    row.who != "agent"
        && (row.who != "recap"
            || rows
                .iter()
                .find(|r| r.who != "agent")
                .is_some_and(|first| first.turn == row.turn))
}
fn terminal_reply(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    last: &Row,
) -> Result<(String, ReplyShape)> {
    let mut text = String::new();
    let mut combined = ReplyShape::default();
    let mut q = c.prepare_cached(
        "SELECT body FROM reply_shapes WHERE source=?1 AND session=?2 AND turn=?3",
    )?;
    for reply in rows.iter().filter(|r| {
        r.who == "agent"
            && (r.turn == last.turn
                || (r.turn > last.turn
                    && rows.iter().any(|p| p.turn == r.turn && p.who == "recap")))
    }) {
        let mut query = q.query(params![reply.source, s.id, reply.source_turn])?;
        let shape = if let Some(row) = query.next()? {
            serde_json::from_str::<ReplyShape>(&row.get::<_, String>(0)?)?
        } else {
            ReplyShape::default()
        };
        let mut offset = text.chars().count() as u32;
        if !text.is_empty() {
            combined.breaks.push(offset);
            combined.worked.push(shape.lead);
            text.push(' ');
            offset += 1;
        }
        combined
            .breaks
            .extend(shape.breaks.iter().map(|p| p + offset));
        combined.worked.extend(shape.worked);
        combined.end = shape.end;
        combined.end_cut = shape.end_cut;
        text.push_str(&reply.text);
    }
    combined.head_chars = text.chars().count() as u32;
    combined.joins = combined.breaks.len() as u32;
    Ok((text, combined))
}
/// What `_terminal` (py/summary.py:711-727) reads for a turn: the text to classify, its
/// blocks, and why its ending is missing.
struct Terminal {
    text: String,
    blocks: Vec<String>,
    worked: Vec<u8>,
    missing: Option<&'static str>,
}
fn terminal(c: &Connection, s: &Session, rows: &[Row], last: &Row) -> Result<Terminal> {
    let (reply, shape) = terminal_reply(c, s, rows, last)?;
    let mut parts = blocks(&reply, &shape.breaks);
    if !shape.end.is_empty() {
        parts.push(shape.end.clone());
        return Ok(Terminal {
            text: shape.end,
            blocks: parts,
            worked: shape.worked,
            missing: shape.end_cut.then_some("reply capped at index time; its final block ran past 8,000 chars, so a list above its ending was not indexed"),
        });
    }
    let missing = (reply.chars().count() > 64_000 && reply.ends_with('…'))
        .then_some("reply capped at index time; ending not indexed");
    Ok(Terminal {
        text: reply,
        blocks: parts,
        worked: shape.worked,
        missing,
    })
}
fn named<'a>(e: &Event<'a>, fallback: &'a str) -> &'a str {
    if e.name.is_empty() {
        fallback
    } else {
        e.name
    }
}
/// Python's `max(events, key=(ts, i))`: the latest, the last recorded on a tie.
fn newest<'a, 'b>(events: impl Iterator<Item = &'a Event<'b>>) -> Option<&'a Event<'b>> {
    events.max_by_key(|e| e.ts)
}
/// 0.4.9's `_question_head` (py/summary.py:1215-1239): what a parked question tool asked.
pub(super) fn question_head(raw: &str, name: &str) -> String {
    let payload = serde_json::from_str::<Value>(raw).ok();
    if let Some(payload) = payload.as_ref().filter(|p| p.is_object()) {
        if let Some(questions) = payload["questions"].as_array() {
            if let Some(q) = questions.iter().find_map(|q| q["question"].as_str()) {
                return one_line(q);
            }
        }
        for key in ["question", "plan", "prompt", "message"] {
            if let Some(text) = payload[key].as_str().filter(|t| !t.trim().is_empty()) {
                return one_line(text);
            }
        }
    }
    for pattern in [
        r#""question"\s*:\s*"((?:[^"\\]|\\.)*)"#,
        r#""plan"\s*:\s*"((?:[^"\\]|\\.)*)"#,
    ] {
        if let Some(found) = re(pattern)
            .captures(raw)
            .filter(|m| !m[1].trim().is_empty())
        {
            // `_json_fragment`: the input cap may have cut the string mid-escape.
            let body = &found[1];
            let ends: Vec<usize> = body
                .char_indices()
                .map(|(at, _)| at)
                .chain([body.len()])
                .collect();
            let decoded = ends.iter().rev().take(6).find_map(|end| {
                serde_json::from_str::<String>(&format!("\"{}\"", &body[..*end])).ok()
            });
            return one_line(&decoded.unwrap_or_else(|| body.to_owned()));
        }
    }
    if payload.is_none() && raw.trim_start().starts_with(['{', '[']) {
        return if name == "ExitPlanMode" {
            "presented a plan"
        } else {
            "asked a question"
        }
        .into();
    }
    one_line(raw)
}
/// 0.4.9's `_api_error` (py/summary.py:1265-1288): the error a Claude turn stopped on.
fn api_error(c: &Connection, s: &Session, later: &[&Event], reply: &str) -> Result<Option<String>> {
    if s.agent != "claude" || reply.is_empty() {
        return Ok(None);
    }
    let Some(event) = newest(later.iter().copied()) else {
        return Ok(None);
    };
    if event.kind != "control" || event.name != "api_error" {
        return Ok(None);
    }
    let text = event.input(c)?;
    let length = text.chars().count();
    let chars = u64::try_from(event.input_chars).unwrap_or(0) as usize;
    let (head, ends) = if text.ends_with('…') && chars >= length {
        let head = one_line(&text.chars().take(length - 1).collect::<String>());
        let reply_chars: Vec<char> = reply.chars().collect();
        let split = reply_chars.len().saturating_sub(chars);
        let tail: String = reply_chars[split..].iter().collect();
        let ends = reply_chars.len() >= chars
            && (split == 0 || reply_chars[split - 1].is_whitespace())
            && one_line(&tail).starts_with(&head);
        (head, ends)
    } else {
        let head = one_line(text);
        let flat = one_line(reply);
        let ends = flat == head || flat.ends_with(&format!(" {head}"));
        (head, ends)
    };
    Ok((!head.is_empty() && ends).then(|| one_line(text)))
}
/// 0.4.9's `_trailing_call` (py/summary.py:1378-1387): the turn's latest work call when no
/// reply followed it.
fn trailing_call<'a, 'b>(later: &[&'a Event<'b>]) -> Option<&'a Event<'b>> {
    let latest = newest(later.iter().copied().filter(|e| {
        let name = e.name;
        e.kind == "tool"
            && !(name.to_lowercase().contains("todo")
                || name.to_lowercase().contains("update_plan")
                || matches!(name, "TaskCreate" | "TaskUpdate" | "TaskList")
                || name.eq_ignore_ascii_case("yield"))
    }))?;
    let after_reply = latest
        .meta
        .split_whitespace()
        .find_map(|part| {
            part.split_once('=')
                .filter(|(key, _)| *key == "after_reply")
        })
        .is_some_and(|(_, value)| value == "1");
    after_reply.then_some(latest)
}
#[derive(Default)]
struct Record {
    status: &'static str,
    confidence: Option<&'static str>,
    signals: Vec<String>,
    evidence: String,
    items: Vec<String>,
    caveats: Vec<String>,
}
fn plural(n: usize) -> &'static str {
    if n == 1 {
        ""
    } else {
        "s"
    }
}
pub(super) fn last_turn(rows: &[Row]) -> Option<&Row> {
    rows.iter().rev().find(|r| is_turn(rows, r))
}
/// 0.4.9's `_classify_root` (py/summary.py:1291-1371): why a root chat still needs the human.
fn classify_root(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
    last: &Row,
) -> Result<Option<Record>> {
    let t = terminal(c, s, rows, last)?;
    let reply = t.text.as_str();
    let later: Vec<&Event> = events
        .iter()
        .filter(|e| last.ts <= 0 || e.ts >= last.ts)
        .collect();
    let mut r = Record::default();
    if let Some(pending) = reply_pending(c, s, &later, &t)? {
        return Ok(Some(pending));
    }
    let failed = later
        .iter()
        .rev()
        .find(|e| e.kind == "tool" && e.ok == Some(false));
    let stopped = later
        .iter()
        .rev()
        .find(|e| e.kind == "control" && re(r"(?i)interrupt|abort|cancel|stop").is_match(e.name));
    let failed_signal = failed.map(|e| format!("latest tool failed ({})", named(e, "tool")));
    let stopped_signal = stopped.map(|e| format!("turn {}", named(e, "stopped")));
    if reply.is_empty() {
        r.signals = vec!["no captured reply to the last turn".into()];
        r.signals.extend(failed_signal);
        r.signals.extend(stopped_signal);
        if last.ts <= 0 {
            r.status = "unknown";
            r.signals.push("last turn has no timestamp".into());
            r.evidence = one_line(&last.text);
        } else {
            r.status = "agent_work_incomplete";
            r.evidence = one_line(match failed {
                Some(e) => e.output(c)?,
                None => &last.text,
            });
        }
        return Ok(Some(r));
    }
    let (open, unknown) = todos(c, events)?;
    r.caveats = unknown.into_iter().collect();
    if !open.is_empty() {
        r.status = "todo_open";
        r.signals = vec![format!(
            "last todo list has {} open item{}",
            open.len(),
            plural(open.len())
        )];
        r.evidence = open[0].clone();
        r.items = open;
        return Ok(Some(r));
    }
    if let Some(trailing) = trailing_call(&later) {
        r.status = "agent_work_incomplete";
        r.signals = vec![format!(
            "reply ended before the turn's last tool call ({})",
            named(trailing, "tool")
        )];
        r.signals.extend(failed_signal);
        r.signals.extend(stopped_signal);
        r.evidence = final_sentence(&prose(reply));
        return Ok(Some(r));
    }
    if let Some(missing) = t.missing {
        r.status = "unknown";
        r.signals = vec![missing.into()];
        r.evidence = final_sentence(&prose(reply));
        return Ok(Some(r));
    }
    if !r.caveats.is_empty() {
        r.status = "unknown";
        r.signals = r.caveats.clone();
        r.evidence = one_line(&last.text);
        return Ok(Some(r));
    }
    Ok(None)
}
/// The body of 0.4.9's `_classify_side` (py/summary.py:1901-1951) once its family checks
/// passed: why a side chat's open work still needs the human.
fn classify_side(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
    last: &Row,
) -> Result<Option<Record>> {
    if last.ts <= 0 {
        return Ok(None);
    }
    let t = terminal(c, s, rows, last)?;
    let later: Vec<&Event> = events.iter().filter(|e| e.ts >= last.ts).collect();
    let mut r = Record {
        status: "agent_work_incomplete",
        ..Default::default()
    };
    if t.text.is_empty() {
        r.signals = vec!["side chat has no captured reply to its last turn".into()];
        if let Some(failed) = later
            .iter()
            .rev()
            .find(|e| e.kind == "tool" && e.ok == Some(false))
        {
            r.signals
                .push(format!("latest tool failed ({})", named(failed, "tool")));
        }
        r.evidence = one_line(&last.text);
        return Ok(Some(r));
    }
    if let Some(error) = api_error(c, s, &later, &t.text)? {
        r.signals = vec!["side chat's final reply is an API error".into()];
        r.evidence = error;
        return Ok(Some(r));
    }
    let (open, unknown) = todos(c, events)?;
    r.caveats = unknown.into_iter().collect();
    if !open.is_empty() {
        r.status = "todo_open";
        r.signals = vec![format!(
            "side chat todo list has {} open item{}",
            open.len(),
            plural(open.len())
        )];
        r.evidence = open[0].clone();
        r.items = open;
        return Ok(Some(r));
    }
    if let Some(trailing) = trailing_call(&later) {
        r.signals = vec![format!(
            "side chat's reply ended before its last tool call ({})",
            named(trailing, "tool")
        )];
        r.evidence = final_sentence(&prose(&t.text));
        return Ok(Some(r));
    }
    r.status = "unknown";
    if let Some(missing) = t.missing {
        r.signals = vec![format!("side chat's {missing}")];
        r.evidence = final_sentence(&prose(&t.text));
        return Ok(Some(r));
    }
    if !r.caveats.is_empty() {
        r.signals = r
            .caveats
            .iter()
            .map(|c| format!("side chat's {c}"))
            .collect();
        r.evidence = one_line(&last.text);
        return Ok(Some(r));
    }
    Ok(None)
}
/// 0.4.9's `_pending_item` (py/summary.py:1955-1993) for one chat's classification.
fn item(s: &Session, last: &Row, r: Record, source: &str) -> Value {
    let confidence = r.confidence.unwrap_or(match r.status {
        "waiting_on_user" | "open_next_steps" => "high",
        "agent_work_incomplete" | "todo_open" => "medium",
        _ => "low",
    });
    json!({"kind":"pending","status":r.status,"confidence":confidence,"project":history::project_leaf(&s.project),"project_label":s.project,"agent":s.agent,"session":s.id,"session_handle":history::target(&s.id),"last_ts":s.last_ts,"turn":last.turn,"handle":history::handle(&s.id,last),"source":source,"signals":r.signals,"evidence":r.evidence.chars().take(240).collect::<String>(),"items":r.items,"first_text":s.title.trim(),"caveats":r.caveats})
}
pub fn pending(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
) -> Result<Option<Value>> {
    let Some(last) = last_turn(rows) else {
        return Ok(None);
    };
    Ok(classify_root(c, s, rows, events, last)?.map(|r| item(s, last, r, "root")))
}
/// A side chat's pending item, worded for its root's briefing.
pub fn side_pending(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
) -> Result<Option<Value>> {
    let Some(last) = last_turn(rows) else {
        return Ok(None);
    };
    Ok(classify_side(c, s, rows, events, last)?.map(|r| item(s, last, r, "side-chat")))
}

fn reply_pending(
    c: &Connection,
    s: &Session,
    later: &[&Event],
    t: &Terminal,
) -> Result<Option<Record>> {
    let reply = t.text.as_str();
    let mut r = Record::default();
    let question = newest(later.iter().copied().filter(|e| e.kind == "tool")).filter(|e| {
        re(r"(?i)^(?:AskUserQuestion|ExitPlanMode|ask|request_user_input|question)$")
            .is_match(e.name)
            && e.ok.is_none()
    });
    if let Some(question) = question {
        if question.output(c)?.trim().is_empty() {
            r.status = "waiting_on_user";
            r.signals = vec![format!("{} awaits your answer", question.name)];
            r.evidence = question_head(question.input(c)?, question.name);
            return Ok(Some(r));
        }
    }
    if let Some(error) = api_error(c, s, later, reply)? {
        r.status = "agent_work_incomplete";
        r.signals = vec!["final reply is an API error".into()];
        r.evidence = error;
        return Ok(Some(r));
    }
    if let Some(request) = Some(reply)
        .filter(|r| !r.is_empty())
        .and_then(direct_request)
    {
        r.status = "waiting_on_user";
        r.signals = vec!["final reply asks you something".into()];
        r.evidence = request;
        return Ok(Some(r));
    }
    if let Some((heading, open, closed, later_block)) = Some(reply)
        .filter(|r| !r.is_empty())
        .and_then(|_| open_section(&t.blocks, &t.worked))
        .filter(|section| !section.1.is_empty())
    {
        let done = if closed > 0 {
            format!(" ({closed} done)")
        } else {
            String::new()
        };
        r.status = "open_next_steps";
        r.signals = vec![format!(
            "{heading} section lists {} open item{}{done}",
            open.len(),
            plural(open.len())
        )];
        r.evidence = open[0].clone();
        r.items = open;
        if let Some(caveat) = later_block {
            r.confidence = Some("medium");
            r.caveats = vec![caveat];
        }
        return Ok(Some(r));
    }
    Ok(None)
}
