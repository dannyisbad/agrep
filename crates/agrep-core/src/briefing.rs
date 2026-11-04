//! Evidence-backed pending items and interval arithmetic for local briefings.
use crate::{
    history::{self, Row, Session},
    model::ReplyShape,
};
use anyhow::Result;
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::BTreeMap;

pub fn events(c: &Connection, s: &Session) -> Result<Vec<Value>> {
    let events = crate::evidence::load_events(c, &s.id, i64::MIN, i64::MAX)?;
    Ok(events
        .into_iter()
        .map(|e| {
            let mut raw = e.raw;
            raw["ts"] = json!(e.ts);
            raw["turn"] = json!(e.turn);
            raw["input"] = json!(e.input);
            raw["output"] = json!(e.output);
            raw["kind"] = json!(e.kind);
            raw["name"] = json!(e.name);
            raw
        })
        .collect())
}

pub fn merge(mut intervals: Vec<(i64, i64)>) -> Vec<(i64, i64)> {
    intervals.sort_unstable();
    let mut out: Vec<(i64, i64)> = vec![];
    for (start, end) in intervals {
        if end <= start {
            continue;
        }
        if let Some(last) = out.last_mut() {
            if start <= last.1 {
                last.1 = last.1.max(end);
                continue;
            }
        }
        out.push((start, end));
    }
    out
}
pub fn span(intervals: &[(i64, i64)]) -> i64 {
    intervals.iter().map(|(s, e)| e - s).sum()
}
pub fn intervals(
    rows: &[Row],
    events: &[Value],
    cap: i64,
    since: i64,
    until: i64,
) -> Vec<(i64, i64)> {
    let mut points: Vec<_> = rows
        .iter()
        .filter(|r| r.who != "agent" && r.ts > 0)
        .map(|r| r.ts)
        .chain(
            events
                .iter()
                .filter(|e| e["name"] == "compacted")
                .filter_map(|e| e["ts"].as_i64()),
        )
        .collect();
    points.sort_unstable();
    points.dedup();
    let mut raw: Vec<_> = points
        .windows(2)
        .map(|w| (w[0], w[1].min(w[0].saturating_add(cap))))
        .collect();
    if let Some(&last) = points.last() {
        if let Some(end) = events
            .iter()
            .filter(|e| e["kind"] == "tool" || e["kind"] == "subagent_result")
            .filter_map(|e| e["ts"].as_i64())
            .max()
        {
            raw.push((last, end.min(last.saturating_add(cap))));
        }
    }
    merge(
        raw.into_iter()
            .map(|(s, e)| (s.max(since), e.min(until)))
            .collect(),
    )
}
fn re(pattern: &'static str) -> regex::Regex {
    use std::sync::{LazyLock, Mutex};
    // Only the finite set of literal patterns in this module enters the cache.
    // A project briefing should compile each expression once, not once per chat.
    static PATTERNS: LazyLock<Mutex<std::collections::HashMap<&'static str, regex::Regex>>> =
        LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));
    PATTERNS
        .lock()
        .expect("pattern cache")
        .entry(pattern)
        .or_insert_with(|| regex::Regex::new(pattern).expect("constant regex"))
        .clone()
}
fn prose(s: &str) -> String {
    let s = re(r"(?s)```.*?```").replace_all(s, "");
    let s = re(r"`[^`\n]*`").replace_all(&s, "");
    s.lines()
        .filter(|s| !s.trim_start().starts_with('>'))
        .collect::<Vec<_>>()
        .join("\n")
}
fn section(s: &str) -> Option<Vec<String>> {
    let text = prose(s);
    let lines: Vec<_> = text.lines().collect();
    let heading = re(
        r"(?i)^\s*(?:#+\s*)?(?:\*\*|__)?\s*(next steps?|remaining(?: work| items?| tasks?| steps?)?|follow[- ]?ups?|to[- ]?dos?|open items?|outstanding(?: work| items?| tasks?)?|still to do|left to do|what'?s left)\s*:?(?:\*\*|__)?\s*:?\s*$",
    );
    let start = lines.iter().rposition(|l| heading.is_match(l))?;
    let bullet = re(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*\S)\s*$");
    let closed =
        re(r"(?i)^\[[x✓✔]\]|^~~.*~~$|(?:done|completed|cancelled|canceled|skipped)\)?\.?$");
    let courtesy = re(r"(?i)let me know|happy to|feel free|say the word");
    let mut items = vec![];
    for line in &lines[start + 1..] {
        if line.trim().is_empty() {
            continue;
        }
        if let Some(m) = bullet.captures(line) {
            let item = &m[1];
            if !closed.is_match(item) && !re(r"(?i)^(none|nothing|n/a|all done)\b").is_match(item) {
                items.push(item.trim_start_matches("[ ]").trim().to_string());
            }
        } else if !courtesy.is_match(line) {
            return None;
        }
    }
    if items.is_empty() {
        None
    } else {
        Some(items)
    }
}
fn block_section(reply: &str, shape: &ReplyShape) -> Option<(Vec<String>, Option<String>)> {
    let chars: Vec<_> = reply.chars().collect();
    let mut blocks = vec![];
    let mut at = 0;
    for offset in &shape.breaks {
        let end = (*offset as usize).min(chars.len());
        if end >= at {
            blocks.push(chars[at..end].iter().collect::<String>());
            at = (end + 1).min(chars.len());
        }
    }
    blocks.push(chars[at..].iter().collect());
    if !shape.end.is_empty() {
        blocks.push(shape.end.clone());
    }
    for (index, block) in blocks.iter().enumerate().rev() {
        if let Some(items) = section(block) {
            if index + 1 == blocks.len() {
                return Some((items, None));
            }
            let joins = &shape.worked[index.min(shape.worked.len())..];
            if joins.contains(&1) {
                return None;
            }
            let later = blocks[index + 1..].join(" ");
            return Some((
                items,
                Some(format!(
                    "a later reply block may report this done: {}",
                    history::one_line(&later, 160)
                )),
            ));
        }
    }
    None
}
#[derive(Clone, Default)]
struct Task {
    id: String,
    phase: String,
    text: String,
    status: String,
}
fn task(item: &Value) -> Task {
    if let Some(text) = item.as_str() {
        return Task {
            text: history::one_line(text, usize::MAX),
            status: "open".into(),
            ..Default::default()
        };
    }
    Task {
        id: match &item["id"] {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            _ => String::new(),
        },
        phase: String::new(),
        text: [
            "content",
            "title",
            "task",
            "step",
            "text",
            "name",
            "description",
            "subject",
        ]
        .iter()
        .find_map(|k| item[*k].as_str().filter(|s| !s.trim().is_empty()))
        .map(|s| history::one_line(s, usize::MAX))
        .unwrap_or_default(),
        status: item["status"]
            .as_str()
            .or_else(|| item["state"].as_str())
            .unwrap_or("")
            .to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join("_"),
    }
}
fn todo_op(tasks: &mut Vec<Task>, payload: &Value) -> bool {
    let items = payload["items"].as_array();
    let phases = payload["list"].as_array();
    let phase = payload["phase"].as_str().unwrap_or("");
    let op = payload["op"].as_str().unwrap_or_else(|| {
        if phases.is_some() {
            "init"
        } else if items.is_some() && !phase.is_empty() {
            "append"
        } else if items.is_some() && tasks.is_empty() {
            "init"
        } else {
            ""
        }
    });
    match op {
        "view" => true,
        "init" => {
            let mut fresh = vec![];
            if let Some(phases) = phases {
                for p in phases {
                    let name = p["phase"].as_str().unwrap_or("Tasks");
                    if let Some(items) = p["items"].as_array() {
                        for i in items {
                            let mut t = task(i);
                            if t.text.is_empty() {
                                continue;
                            }
                            t.phase = name.into();
                            t.status = "pending".into();
                            fresh.push(t);
                        }
                    }
                }
            } else if let Some(items) = items {
                for i in items {
                    let mut t = task(i);
                    if t.text.is_empty() {
                        continue;
                    }
                    t.phase = if phase.is_empty() { "Tasks" } else { phase }.into();
                    t.status = "pending".into();
                    fresh.push(t);
                }
            } else {
                return false;
            }
            *tasks = fresh;
            true
        }
        "append" => {
            let Some(items) = items else { return false };
            if phase.is_empty() {
                return false;
            }
            let fresh: Vec<_> = items
                .iter()
                .map(task)
                .filter(|t| !t.text.is_empty())
                .collect();
            if fresh.iter().any(|f| tasks.iter().any(|t| t.text == f.text)) {
                return false;
            }
            for mut t in fresh {
                t.phase = phase.into();
                t.status = "pending".into();
                tasks.push(t);
            }
            true
        }
        "start" | "done" | "drop" | "block" | "unblock" | "rm" => {
            let target = payload["task"].as_str().unwrap_or("");
            if (op == "start" && target.is_empty())
                || (matches!(op, "block" | "unblock") && target.is_empty() && phase.is_empty())
            {
                return false;
            }
            let hits: Vec<_> = tasks
                .iter()
                .enumerate()
                .filter(|(_, t)| {
                    if !target.is_empty() {
                        t.text == target
                    } else if !phase.is_empty() {
                        t.phase == phase
                    } else {
                        true
                    }
                })
                .map(|(i, _)| i)
                .collect();
            if hits.is_empty() {
                return false;
            }
            if op == "start" {
                for t in tasks.iter_mut() {
                    if t.status == "in_progress" {
                        t.status = "pending".into();
                    }
                }
            }
            if op == "rm" {
                for i in hits.into_iter().rev() {
                    tasks.remove(i);
                }
                return true;
            }
            for i in hits {
                let t = &mut tasks[i];
                let state = match op {
                    "start" => "in_progress",
                    "done" => "completed",
                    "drop" => "abandoned",
                    "block"
                        if matches!(t.status.as_str(), "pending" | "in_progress" | "blocked") =>
                    {
                        "blocked"
                    }
                    "unblock" if t.status == "blocked" => "pending",
                    _ => continue,
                };
                t.status = state.into();
            }
            true
        }
        _ => false,
    }
}
fn todos(events: &[Value]) -> (Vec<String>, Option<String>) {
    let mut tasks: Vec<Task> = vec![];
    let mut claude = BTreeMap::<String, Task>::new();
    let mut claude_last = false;
    let mut unknown = None;
    let mut claude_unknown = None;
    for event in events {
        let name = event["name"].as_str().unwrap_or("");
        if !name.to_lowercase().contains("todo")
            && !matches!(
                name,
                "update_plan" | "TaskCreate" | "TaskUpdate" | "TaskList"
            )
        {
            continue;
        }
        if event["ok"] == false {
            continue;
        }
        let raw = event["state"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or_else(|| event["input"].as_str())
            .unwrap_or("");
        let state = serde_json::from_str::<Value>(raw).ok();
        if name.starts_with("Task") {
            claude_last = true;
            let output = event["output"].as_str().unwrap_or("");
            match name {
                "TaskCreate" => {
                    if let Some(m) =
                        re(r"(?s)^Task #(\S+) created successfully: (.*)").captures(output)
                    {
                        claude.insert(
                            m[1].into(),
                            Task {
                                id: m[1].into(),
                                text: history::one_line(&m[2], usize::MAX),
                                status: "pending".into(),
                                ..Default::default()
                            },
                        );
                    } else {
                        claude_unknown = Some("task creation result cannot be resolved".into());
                    }
                }
                "TaskUpdate" => {
                    if !output.is_empty() && !output.starts_with("Updated task #") {
                        continue;
                    }
                    let update = re(r"^Updated task #(\S+)").captures(output);
                    let id = update
                        .as_ref()
                        .map(|m| m[1].to_string())
                        .or_else(|| {
                            state.as_ref().and_then(|s| {
                                ["taskId", "id", "task_id"]
                                    .iter()
                                    .find_map(|k| s[*k].as_str().map(str::to_string))
                            })
                        })
                        .unwrap_or_default();
                    if let Some(s) = state.as_ref() {
                        if s["status"] == "deleted" {
                            claude.remove(&id);
                            continue;
                        }
                        if let Some(t) = claude.get_mut(&id) {
                            if let Some(status) = s["status"].as_str() {
                                t.status = status.into()
                            }
                            if let Some(subject) = s["subject"].as_str() {
                                t.text = subject.into()
                            }
                        } else {
                            claude_unknown = Some("task update refers to an unknown task".into());
                        }
                    } else {
                        claude_unknown = Some("task update payload is incomplete".into());
                    }
                }
                "TaskList" => {
                    let listing = state
                        .as_ref()
                        .and_then(|s| s["listing"].as_str())
                        .unwrap_or(output);
                    if listing.is_empty() {
                        continue;
                    }
                    let mut fresh = BTreeMap::new();
                    let mut valid = true;
                    for line in listing.lines() {
                        if let Some(m) = re(r"^#(\S+) \[([a-z_]+)\](?: (.*))?$").captures(line) {
                            let text = claude
                                .get(&m[1])
                                .map(|t| t.text.clone())
                                .unwrap_or_else(|| m.get(3).map_or("", |s| s.as_str()).into());
                            fresh.insert(
                                m[1].into(),
                                Task {
                                    id: m[1].into(),
                                    text,
                                    status: m[2].into(),
                                    ..Default::default()
                                },
                            );
                        } else if line != "No tasks found" {
                            valid = false;
                        }
                    }
                    if valid {
                        claude = fresh;
                        claude_unknown = None
                    } else {
                        claude_unknown = Some("task listing is incomplete".into())
                    }
                }
                _ => {}
            }
            continue;
        }
        claude_last = false;
        let Some(state) = state else {
            unknown = Some("task-list payload is incomplete".into());
            continue;
        };
        if state == json!({}) {
            continue;
        }
        let whole = state
            .as_array()
            .or_else(|| ["todos", "plan"].iter().find_map(|k| state[*k].as_array()));
        if let Some(items) = whole {
            let incoming: Vec<_> = items.iter().map(task).collect();
            if state["merge"] == true {
                for incoming in incoming {
                    if let Some(t) = tasks
                        .iter_mut()
                        .find(|t| !incoming.id.is_empty() && t.id == incoming.id)
                    {
                        if !incoming.text.is_empty() {
                            t.text = incoming.text;
                        }
                        if !incoming.status.is_empty() {
                            t.status = incoming.status;
                        }
                    } else if !incoming.text.is_empty() {
                        tasks.push(incoming)
                    } else {
                        unknown = Some("merged task has no known content".into());
                    }
                }
            } else {
                tasks = incoming
                    .into_iter()
                    .filter(|t| !t.text.is_empty())
                    .collect();
                unknown = None;
            }
        } else if todo_op(&mut tasks, &state) {
            if state["op"] == "init" || state.get("list").is_some() {
                unknown = None
            }
        } else {
            unknown = Some("task operation cannot be resolved".into())
        }
    }
    let (tasks, unknown) = if claude_last {
        (claude.into_values().collect::<Vec<_>>(), claude_unknown)
    } else {
        (tasks, unknown)
    };
    let open = tasks
        .into_iter()
        .filter(|t| {
            matches!(
                t.status.as_str(),
                "pending"
                    | "in_progress"
                    | "in-progress"
                    | "not_started"
                    | "not-started"
                    | "open"
                    | "todo"
                    | "active"
                    | "blocked"
                    | "queued"
            )
        })
        .map(|t| t.text)
        .collect();
    if unknown.is_some() {
        (vec![], unknown)
    } else {
        (open, unknown)
    }
}
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
    let mut q =
        c.prepare("SELECT body FROM reply_shapes WHERE source=?1 AND session=?2 AND turn=?3")?;
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
pub fn pending(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Value],
) -> Result<Option<Value>> {
    let Some(last) = rows.iter().rev().find(|r| is_turn(rows, r)) else {
        return Ok(None);
    };
    let (reply, shape) = terminal_reply(c, s, rows, last)?;
    let text = if shape.end.is_empty() {
        reply.as_str()
    } else {
        shape.end.as_str()
    };
    let tail = prose(text);
    let final_sentence = tail.trim().rsplit(['\n']).next().unwrap_or("");
    let mut status = "";
    let mut confidence = "medium";
    let mut evidence = String::new();
    let todo_signal: String;
    let mut signals = vec![];
    let mut items = vec![];
    let mut caveats = vec![];
    let later: Vec<_> = events
        .iter()
        .filter(|e| e["ts"].as_i64().unwrap_or(0) >= last.ts)
        .collect();
    if let Some(e)=later.iter().rev().find(|e|e["name"].as_str().is_some_and(|n|matches!(n,"AskUserQuestion"|"ExitPlanMode"|"ask"|"question"|"request_user_input"))&&e["output"].as_str().unwrap_or("").is_empty()){
        status="waiting_on_user";confidence="high";evidence=e["input"].as_str().unwrap_or("").into();signals.push("question tool awaits your answer");
    }else if later.iter().any(|e|e["name"]=="api_error"){
        status="agent_work_incomplete";evidence=text.into();signals.push("final reply is an API error");
    }else if (final_sentence.ends_with('?')&&re(r"(?i)should i|shall i|do you want|would you like|can you|could you|please confirm|which|prefer|want me to|how should|where should").is_match(final_sentence))||re(r"(?i)please confirm|awaiting your|waiting for your|waiting on you|your call").is_match(final_sentence){
        status="waiting_on_user";confidence="high";evidence=final_sentence.into();signals.push("final reply asks you something");
    }else if let Some((open,caveat))=block_section(&reply,&shape){status="open_next_steps";confidence=if caveat.is_some(){"medium"}else{"high"};evidence=open[0].clone();items=open;signals.push("final reply lists open next steps");if let Some(c)=caveat{caveats.push(c);}
    }else if reply.is_empty(){status=if last.ts>0{"agent_work_incomplete"}else{"unknown"};confidence=if last.ts>0{"medium"}else{"low"};evidence=last.text.clone();signals.push("no captured reply to the last turn");
    }else{
        let (open,unknown)=todos(events);if !open.is_empty(){status="todo_open";evidence=open[0].clone();todo_signal=format!("last todo list has {} open item{}",open.len(),if open.len()==1{""}else{"s"});signals.push(todo_signal.as_str());items=open;}
        else if later.iter().any(|e|e["meta"].as_str().unwrap_or("").split_whitespace().any(|m|m=="after_reply=1")){status="agent_work_incomplete";evidence=final_sentence.into();signals.push("reply ended before the turn's last tool call");}
        else if let Some(reason)=unknown{status="unknown";confidence="low";evidence=final_sentence.into();caveats.push(reason);}
        else if shape.end_cut{status="unknown";confidence="low";evidence=final_sentence.into();caveats.push("reply ending was capped; omitted items cannot be classified".into());}
    }
    if status.is_empty() {
        return Ok(None);
    }
    Ok(Some(
        json!({"kind":"pending","status":status,"confidence":confidence,"project":history::project_leaf(&s.project),"project_label":s.project,"agent":s.agent,"session":s.id,"session_handle":history::target(&s.id),"last_ts":s.last_ts,"turn":last.turn,"handle":history::handle(&s.id,last),"source":"root","signals":signals,"evidence":history::one_line(&evidence,240),"items":items,"first_text":s.title,"caveats":caveats}),
    ))
}

pub fn returned(rows: &[Row], events: &[Value]) -> bool {
    let Some(last) = events
        .iter()
        .filter(|e| matches!(e["kind"].as_str(), Some("tool" | "subagent_result")))
        .max_by_key(|e| e["ts"].as_i64().unwrap_or(0))
    else {
        return false;
    };
    if !last["name"]
        .as_str()
        .is_some_and(|s| s.eq_ignore_ascii_case("yield"))
        || last["ok"] == false
    {
        return false;
    }
    if rows
        .iter()
        .any(|r| r.who != "agent" && r.ts > last["ts"].as_i64().unwrap_or(0))
    {
        return false;
    }
    let input = last["input"].as_str().unwrap_or("");
    !serde_json::from_str::<Value>(input)
        .ok()
        .is_some_and(|v| v["type"].is_array())
}
pub fn received(events: &[Value], side: &Session, rows: &[Row]) -> bool {
    let final_text = rows
        .iter()
        .rev()
        .find(|r| r.who == "agent")
        .map(|r| history::one_line(&r.text, usize::MAX))
        .unwrap_or_default();
    events.iter().any(|e| {
        let child = e["child_session"].as_str().unwrap_or("");
        let output = e["output"].as_str().unwrap_or("");
        (child == side.id && e["kind"] == "subagent_result" && e["ok"] != false)
            || ((child.is_empty() || child == side.id)
                && matches!(
                    e["name"].as_str(),
                    Some("task" | "agent" | "subagent" | "wait_agent" | "Task")
                )
                && final_text.len() >= 20
                && history::one_line(output, usize::MAX).contains(&final_text))
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn event(name: &str, state: Value) -> Value {
        json!({"name":name,"kind":"tool","ok":true,"state":state.to_string()})
    }
    #[test]
    fn whole_lists_and_partial_merges_preserve_fields() {
        let initial = event(
            "todo_write",
            json!({"todos":[{"id":"a","content":"keep name","status":"in_progress"},{"id":2,"content":"second","status":"pending"}]}),
        );
        let merge = event(
            "todo_write",
            json!({"merge":true,"todos":[{"id":"a","status":"completed"}]}),
        );
        assert_eq!(todos(&[initial, merge]).0, vec!["second"]);
        assert_eq!(
            todos(&[event(
                "write_todos",
                json!({"todos":[{"description":"Gemini task","status":"In Progress"}]})
            )])
            .0,
            vec!["Gemini task"]
        );
    }
    #[test]
    fn phase_operations_and_failed_updates_are_evidence() {
        let init = event(
            "todo",
            json!({"list":[{"phase":"build","items":["compile","test"]}]}),
        );
        let done = event("todo", json!({"op":"done","phase":"build"}));
        assert!(todos(&[init.clone(), done]).0.is_empty());
        let mut failed = event("todo", json!({"op":"done","phase":"build"}));
        failed["ok"] = json!(false);
        assert_eq!(todos(&[init, failed]).0, vec!["compile", "test"]);
    }
    #[test]
    fn completed_checklists_and_later_work_do_not_reopen() {
        assert!(section("Next steps:\n- [x] done\n- None").is_none());
        let text = "Next steps:\n- run tests All tests passed.";
        let shape = ReplyShape {
            breaks: vec![23],
            worked: vec![1],
            ..Default::default()
        };
        assert!(block_section(text, &shape).is_none());
    }
    #[test]
    fn interval_union_counts_overlap_once() {
        assert_eq!(
            merge(vec![(0, 20), (10, 30), (30, 40), (50, 50)]),
            vec![(0, 40)]
        );
    }
}
