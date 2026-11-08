//! Task-list calls: which calls write or read the agent's task list, which are bookkeeping
//! rather than work, and the task-list facts kept whole past the display cap.

use serde_json::Value;

use super::REPLY_CAP;

/// Element fields of a todo/plan list kept in a task state.
const ELEMENT_KEYS: &[&str] = &[
    "content",
    "title",
    "task",
    "step",
    "text",
    "name",
    "description",
    "status",
    "state",
    "id",
];

/// Scalar top-level fields of a task call kept in a task state.
const FIELD_KEYS: &[&str] = &[
    "op", "task", "phase", "subject", "status", "taskId", "id", "task_id",
];

/// Is this a call that writes or reads the agent's task list? Claude TodoWrite/TaskUpdate,
/// opencode todowrite, Cursor todo_write, omp todo, Codex update_plan, Kimi SetTodoList,
/// Gemini write_todos.
pub fn is_task_tool(name: &str) -> bool {
    name == "TaskUpdate" || name == "update_plan" || name.to_ascii_lowercase().contains("todo")
}

/// A call that is not the agent's work: a todo/plan/task-list write or a `yield`, as summary
/// reads `after_reply` (its `_bookkeeping_call`).
pub fn is_bookkeeping_tool(name: &str) -> bool {
    is_task_tool(name)
        || name == "TaskCreate"
        || name == "TaskList"
        || name.eq_ignore_ascii_case("yield")
}

/// A recorded call that is the agent's work: a tool or a delegation, never bookkeeping.
pub fn is_work_event(event: &crate::model::Event) -> bool {
    (event.kind == "tool" || event.kind == "subagent_start") && !is_bookkeeping_tool(&event.name)
}

/// The task-list facts a call carried, kept whole where its display input is capped: the
/// todo/plan elements' names, statuses and ids, Cursor's merge flag, omp's todo op with its
/// targets, a Claude TaskUpdate's fields. Compact JSON with sorted keys, so summary reads it as
/// it reads an uncapped input; empty for other calls, for inputs carrying none of those facts,
/// and past `REPLY_CAP` characters.
pub fn task_state(name: &str, input: Option<&Value>) -> String {
    if !is_task_tool(name) {
        return String::new();
    }
    let state = match input {
        Some(Value::Array(items)) => Value::Array(elements(items)),
        Some(Value::Object(fields)) => Value::Object(task_fields(fields)),
        _ => return String::new(),
    };
    let empty = match &state {
        Value::Array(items) => items.is_empty(),
        Value::Object(fields) => fields.is_empty(),
        _ => true,
    };
    if empty {
        return String::new();
    }
    let encoded = state.to_string();
    if encoded.chars().count() > REPLY_CAP {
        return String::new();
    }
    encoded
}

/// The whole list a Claude TaskList result printed, kept past the display cap so the replay
/// can reset to it; empty when the listing itself exceeds `REPLY_CAP` characters.
pub fn task_listing(output: &str) -> String {
    if output.trim().is_empty() || output.chars().count() > REPLY_CAP {
        return String::new();
    }
    serde_json::json!({ "listing": output }).to_string()
}

fn scalar(value: &Value) -> bool {
    matches!(value, Value::String(_) | Value::Number(_))
}

/// A todo/plan list's elements: strings whole, objects cut to their scalar `ELEMENT_KEYS`.
fn elements(items: &[Value]) -> Vec<Value> {
    items
        .iter()
        .filter_map(|item| match item {
            Value::String(_) => Some(item.clone()),
            Value::Object(fields) => Some(Value::Object(
                fields
                    .iter()
                    .filter(|(key, value)| ELEMENT_KEYS.contains(&key.as_str()) && scalar(value))
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            )),
            _ => None,
        })
        .collect()
}

fn string_items(items: &[Value]) -> Value {
    Value::Array(
        items
            .iter()
            .filter(|item| item.is_string())
            .cloned()
            .collect(),
    )
}

/// A task call's object input cut to the task-list facts it carries.
fn task_fields(fields: &serde_json::Map<String, Value>) -> serde_json::Map<String, Value> {
    let mut kept = serde_json::Map::new();
    for (key, value) in fields {
        if let Some(value) = task_field(key, value) {
            kept.insert(key.clone(), value);
        }
    }
    kept
}

fn task_field(key: &str, value: &Value) -> Option<Value> {
    Some(match (key, value) {
        ("todos" | "plan", Value::Array(items)) => Value::Array(elements(items)),
        ("items", Value::Array(items)) => string_items(items),
        ("list", Value::Array(phases)) => Value::Array(phases.iter().filter_map(phase).collect()),
        ("merge", Value::Bool(_)) => value.clone(),
        (key, value) if FIELD_KEYS.contains(&key) && scalar(value) => value.clone(),
        _ => return None,
    })
}

/// One phase of a phased task list: its string name and its string items.
fn phase(phase: &Value) -> Option<Value> {
    let fields = phase.as_object()?;
    let mut kept = serde_json::Map::new();
    if let Some(name) = fields.get("phase").filter(|v| v.is_string()) {
        kept.insert("phase".to_string(), name.clone());
    }
    if let Some(Value::Array(items)) = fields.get("items") {
        kept.insert("items".to_string(), string_items(items));
    }
    Some(Value::Object(kept))
}
