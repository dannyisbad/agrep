//! Replay whole task lists, phase operations and Claude task events.
use super::{prose::re, Event};
use crate::history;
use anyhow::Result;
use rusqlite::Connection;
use serde_json::{json, Value};
use std::collections::BTreeMap;

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
        "init" => init_tasks(tasks, phases, items, phase),
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
            update_tasks(tasks, payload, op, phase)
        }
        _ => false,
    }
}
pub(super) fn todos(c: &Connection, events: &[Event]) -> Result<(Vec<String>, Option<String>)> {
    let mut tasks: Vec<Task> = vec![];
    let mut claude = ClaudeTasks::default();
    let mut claude_last = false;
    let mut unknown = None;
    for event in events {
        let name = event.name;
        if !name.to_lowercase().contains("todo")
            && !matches!(
                name,
                "update_plan" | "TaskCreate" | "TaskUpdate" | "TaskList"
            )
        {
            continue;
        }
        if event.ok == Some(false) {
            continue;
        }
        let raw = if event.state.is_empty() {
            event.input(c)?
        } else {
            event.state.as_str()
        };
        let state = serde_json::from_str::<Value>(raw).ok();
        if name.starts_with("Task") {
            claude_last = true;
            let output = event.output(c)?;
            claude.apply(name, state.as_ref(), output);
            continue;
        }
        claude_last = false;
        replay_list(&mut tasks, &mut unknown, state);
    }
    let (tasks, unknown) = if claude_last {
        (
            claude.tasks.into_values().collect::<Vec<_>>(),
            claude.unknown,
        )
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
    Ok(if unknown.is_some() {
        (vec![], unknown)
    } else {
        (open, unknown)
    })
}

fn replay_list(tasks: &mut Vec<Task>, unknown: &mut Option<String>, state: Option<Value>) {
    let Some(state) = state else {
        *unknown = Some("task-list payload is incomplete".into());
        return;
    };
    if state == json!({}) {
        return;
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
                    *unknown = Some("merged task has no known content".into());
                }
            }
        } else {
            *tasks = incoming
                .into_iter()
                .filter(|t| !t.text.is_empty())
                .collect();
            *unknown = None;
        }
    } else if todo_op(tasks, &state) {
        if state["op"] == "init" || state.get("list").is_some() {
            *unknown = None
        }
    } else {
        *unknown = Some("todo list uses an op this build cannot replay".into())
    }
}

#[derive(Default)]
struct ClaudeTasks {
    tasks: BTreeMap<String, Task>,
    unknown: Option<String>,
}

impl ClaudeTasks {
    fn apply(&mut self, name: &str, state: Option<&Value>, output: &str) {
        match name {
            "TaskCreate" => self.create(output),
            "TaskUpdate" => self.update(state, output),
            "TaskList" => self.list(state, output),
            _ => {}
        }
    }
    fn create(&mut self, output: &str) {
        if let Some(m) = re(r"(?s)^Task #(\S+) created successfully: (.*)").captures(output) {
            self.tasks.insert(
                m[1].into(),
                Task {
                    id: m[1].into(),
                    text: history::one_line(&m[2], usize::MAX),
                    status: "pending".into(),
                    ..Default::default()
                },
            );
        } else {
            self.unknown = Some("task creation result cannot be resolved".into());
        }
    }
    fn update(&mut self, state: Option<&Value>, output: &str) {
        if !output.is_empty() && !output.starts_with("Updated task #") {
            return;
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
                self.tasks.remove(&id);
                return;
            }
            if let Some(t) = self.tasks.get_mut(&id) {
                if let Some(status) = s["status"].as_str() {
                    t.status = status.into()
                }
                if let Some(subject) = s["subject"].as_str() {
                    t.text = subject.into()
                }
            } else {
                self.unknown = Some("task update refers to an unknown task".into());
            }
        } else {
            self.unknown = Some("task update payload is incomplete".into());
        }
    }
    fn list(&mut self, state: Option<&Value>, output: &str) {
        let listing = state
            .as_ref()
            .and_then(|s| s["listing"].as_str())
            .unwrap_or(output);
        if listing.is_empty() {
            return;
        }
        let mut fresh = BTreeMap::new();
        let mut valid = true;
        for line in listing.lines() {
            if let Some(m) = re(r"^#(\S+) \[([a-z_]+)\](?: (.*))?$").captures(line) {
                let text = self
                    .tasks
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
            self.tasks = fresh;
            self.unknown = None
        } else {
            self.unknown = Some("task listing is incomplete".into())
        }
    }
}

fn init_tasks(
    tasks: &mut Vec<Task>,
    phases: Option<&Vec<Value>>,
    items: Option<&Vec<Value>>,
    phase: &str,
) -> bool {
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

fn update_tasks(tasks: &mut Vec<Task>, payload: &Value, op: &str, phase: &str) -> bool {
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
            "block" if matches!(t.status.as_str(), "pending" | "in_progress" | "blocked") => {
                "blocked"
            }
            "unblock" if t.status == "blocked" => "pending",
            _ => continue,
        };
        t.status = state.into();
    }
    true
}
