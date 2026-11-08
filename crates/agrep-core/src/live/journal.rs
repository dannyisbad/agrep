use super::{cap, stamp, text};
use serde_json::{json, Value};
use std::path::Path;
#[derive(Clone, Default)]
pub(crate) struct Meta {
    pub session: String,
    pub project: String,
    pub model: String,
    pub parent: String,
    pub title: String,
    pub side: bool,
    initialized: bool,
    started: bool,
    start_turn: String,
    agent_path: String,
    replayed: bool,
    guardian: bool,
    seen: std::collections::HashSet<String>,
    announced: std::collections::VecDeque<(String, String)>,
}
impl Meta {
    pub fn from_path(agent: &str, path: &Path) -> Self {
        let stem = path
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        let session = if agent == "antigravity" {
            path.ancestors()
                .find(|p| p.join(".system_generated").is_dir())
                .and_then(|p| p.file_name())
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned()
        } else {
            stem
        };
        let mut m = Self {
            session,
            side: path.components().any(|c| c.as_os_str() == "subagents"),
            ..Self::default()
        };
        if agent == "pi" {
            if let Some(parent) = path
                .parent()
                .and_then(|p| p.file_name())
                .and_then(|s| s.to_str())
                .filter(|s| s.contains('_'))
            {
                m.parent = parent.rsplit('_').next().unwrap_or(parent).into();
                m.side = true;
            }
            if let Some(slug) = path
                .components()
                .filter_map(|p| p.as_os_str().to_str())
                .find(|s| s.starts_with("--") && s.ends_with("--"))
            {
                m.project = slug.trim_matches('-').replace('-', "/");
            }
        }
        m
    }
    pub fn header(&mut self, agent: &str, v: &Value) {
        let kind = text(&v["type"]);
        let p = if agent == "codex" { &v["payload"] } else { v };
        if (agent == "codex" && kind == "session_meta") || (agent == "pi" && kind == "session") {
            if let Some(id) = p["id"].as_str() {
                if agent == "codex" && self.initialized && self.side && id != self.session {
                    self.parent = id.into();
                    self.replayed = true;
                } else if !self.initialized {
                    self.session = id.into();
                }
            }
            if agent == "codex" && !self.initialized {
                self.side = p["thread_source"] == "subagent" || p["source"]["subagent"].is_object();
                self.guardian = p["source"]["subagent"]["other"] == "guardian";
                self.agent_path = p["agent_path"]
                    .as_str()
                    .or(p["source"]["subagent"]["thread_spawn"]["agent_path"].as_str())
                    .unwrap_or("")
                    .into();
            }
            self.initialized = true;
            let parent = p["parentSessionId"]
                .as_str()
                .or(p["parentSession"].as_str())
                .or(p["parent_session_id"].as_str())
                .or(p["parent_thread_id"].as_str())
                .or(p["source"]["subagent"]["thread_spawn"]["parent_thread_id"].as_str());
            if let Some(s) = parent {
                self.parent = s.into();
                self.side = true;
            }
        }
        if agent == "claude" {
            if let Some(id) = v["sessionId"].as_str() {
                if self.side {
                    self.parent = id.into()
                } else {
                    self.session = id.into()
                }
            }
        }
        if let Some(cwd) = p["cwd"].as_str().filter(|s| !s.is_empty()) {
            if self.project.is_empty() || agent == "pi" {
                self.project = crate::ingest::project_name(cwd);
            }
        }
        if agent == "pi" && matches!(kind, "title" | "title_change") {
            if let Some(s) = v["title"].as_str() {
                self.title = s.into()
            }
        }
        if kind == "turn_context" || kind == "model_change" {
            if let Some(s) = p["modelId"].as_str().or(p["model"].as_str()) {
                self.model = s.into()
            }
        }
    }
    fn base(&self, agent: &str, ts: i64) -> Value {
        json!({"agent":agent,"session":self.session,"project":self.project,"model":self.model,"parent":self.parent,"sub_session":self.side,"title":self.title,"ts":ts})
    }
}
fn event(base: &Value, kind: &str, fields: Value) -> Value {
    let mut v = base.clone();
    v["type"] = json!(kind);
    if let Some(map) = fields.as_object() {
        for (k, x) in map {
            v[k] = x.clone();
        }
    }
    v
}
fn content(v: &Value) -> String {
    if let Some(s) = v.as_str() {
        return s.into();
    }
    v.as_array()
        .map(|rows| {
            rows.iter()
                .filter(|x| matches!(text(&x["type"]), "text" | "input_text" | "output_text"))
                .filter_map(|x| x["text"].as_str())
                .collect::<Vec<_>>()
                .join("\n")
        })
        .unwrap_or_default()
}
fn noise(s: &str, codex: bool) -> bool {
    s.trim().is_empty()
        || crate::ingest::is_wrapper(s)
        || (codex && crate::ingest::codex::is_codex_injected(s))
}

fn input(v: &Value) -> String {
    let parsed;
    let v = if let Some(s) = v.as_str() {
        parsed = serde_json::from_str::<Value>(s).unwrap_or_else(|_| v.clone());
        &parsed
    } else {
        v
    };
    crate::ingest::summarize_tool_input(v)
}
fn output(v: &Value) -> (String, Option<bool>) {
    let parsed;
    let v = if let Some(s) = v.as_str() {
        parsed = serde_json::from_str::<Value>(s).unwrap_or_else(|_| v.clone());
        &parsed
    } else {
        v
    };
    if v.is_object() {
        let s = v["output"]
            .as_str()
            .or(v["aggregated_output"].as_str())
            .unwrap_or("");
        (
            cap(s, 400),
            v["metadata"]["exit_code"]
                .as_i64()
                .or(v["exit_code"].as_i64())
                .map(|n| n == 0),
        )
    } else {
        (cap(&content(v), 400), None)
    }
}
pub(crate) fn parse(agent: &str, m: &mut Meta, v: &Value) -> Vec<Value> {
    m.header(agent, v);
    if m.guardian {
        return vec![];
    }
    let mut out = vec![];
    let mut ts = stamp(&v["timestamp"]);
    if ts == 0 {
        ts = stamp(&v["created_at"])
    }
    let base = m.base(agent, ts);
    match agent {
        "claude" | "pi" => message(agent, m, v, base, &mut out),
        "codex" => codex(m, v, &base, &mut out),
        "antigravity" => antigravity(m, v, &base, &mut out),
        _ => {}
    }
    out
}
fn blocks(content: &Value) -> impl Iterator<Item = &Value> {
    content.as_array().into_iter().flatten()
}
/// Claude and pi journal records.
fn message(agent: &str, m: &mut Meta, v: &Value, mut base: Value, out: &mut Vec<Value>) {
    let kind = text(&v["type"]);
    if agent == "claude" && v["isSidechain"] == true && !m.side {
        return;
    }
    if kind == "queue-operation" {
        out.push(event(
            &base,
            "queued",
            json!({"op":v["operation"],"text":cap(text(&v["content"]),160)}),
        ));
        return;
    }
    if kind == "custom" && v["customType"] == "session_exit" {
        out.push(event(
            &base,
            "done",
            json!({"why":if v["data"]["kind"]=="signal"{"interrupted"}else{"done"}}),
        ));
        return;
    }
    let msg = &v["message"];
    let role = if agent == "pi" {
        text(&msg["role"])
    } else {
        kind
    };
    if agent == "pi" && kind != "message" {
        return;
    }
    if let Some(model) = msg["model"].as_str().filter(|s| !s.starts_with('<')) {
        m.model = model.into();
        base["model"] = json!(model)
    }
    if agent == "pi" && !msg["timestamp"].is_null() {
        base["ts"] = json!(stamp(&msg["timestamp"]))
    }
    match role {
        "assistant" => assistant(agent, &base, msg, out),
        "user" => user(&base, v, &msg["content"], out),
        "toolResult" => out.push(event(
            &base,
            "tool_done",
            json!({"call_id":msg["toolCallId"],"name":msg["toolName"],"output":cap(&content(&msg["content"]),400),"ok":msg["isError"].as_bool().map(|v|!v)}),
        )),
        "bashExecution" => {
            out.push(event(
                &base,
                "tool",
                json!({"name":"bash","input":cap(text(&msg["command"]),400),"call_id":v["id"]}),
            ));
            out.push(event(
                &base,
                "tool_done",
                json!({"name":"bash","output":cap(text(&msg["output"]),400),"call_id":v["id"],"ok":msg["exitCode"].as_i64().map(|v|v==0 && msg["cancelled"]!=true)}),
            ));
        }
        _ => {}
    }
}
fn assistant(agent: &str, base: &Value, msg: &Value, out: &mut Vec<Value>) {
    let c = &msg["content"];
    if let Some(s) = c.as_str().filter(|s| !s.trim().is_empty()) {
        out.push(event(base, "reply", json!({"text":cap(s,4000)})))
    }
    for b in blocks(c) {
        match text(&b["type"]) {
            "text" if !text(&b["text"]).trim().is_empty() => out.push(event(
                base,
                "reply",
                json!({"text":cap(text(&b["text"]),4000)}),
            )),
            "tool_use" | "toolCall" => {
                let arguments = if agent == "pi" {
                    &b["arguments"]
                } else {
                    &b["input"]
                };
                out.push(event(
                    base,
                    "tool",
                    json!({"name":b["name"],"call_id":b["id"],"input":input(arguments)}),
                ))
            }
            _ => {}
        }
    }
    let stop = msg["stopReason"]
        .as_str()
        .or(msg["stop_reason"].as_str())
        .unwrap_or("");
    if (!stop.is_empty() && agent == "pi" && !matches!(stop, "toolUse" | "tool_use"))
        || matches!(stop, "end_turn" | "stop_sequence" | "max_tokens")
    {
        let fields = if matches!(stop, "aborted" | "error") {
            json!({"why":"interrupted"})
        } else {
            json!({})
        };
        out.push(event(base, "done", fields))
    }
}
fn user(base: &Value, v: &Value, c: &Value, out: &mut Vec<Value>) {
    for b in blocks(c).filter(|b| b["type"] == "tool_result") {
        out.push(event(
            base,
            "tool_done",
            json!({"call_id":b["tool_use_id"],"ok":b["is_error"]!=true,"output":cap(&content(&b["content"]),400)}),
        ))
    }
    let s = content(c);
    if s.trim_start().starts_with("[Request interrupted") {
        out.push(event(base, "done", json!({"why":"interrupted"})))
    } else if v["isMeta"] != true
        && !noise(&s, false)
        && !matches!(v["userType"].as_str(), Some(t) if !t.is_empty() && t != "external")
    {
        out.push(event(base, "user", json!({"text":cap(&s,4000)})))
    }
}
fn codex(m: &mut Meta, v: &Value, base: &Value, out: &mut Vec<Value>) {
    if m.side && codex_side(m, v, base, out) {
        return;
    }
    let p = &v["payload"];
    match (text(&v["type"]), text(&p["type"])) {
        ("event_msg", "task_complete") => out.push(event(base, "done", json!({}))),
        ("event_msg", "turn_aborted") => {
            out.push(event(base, "done", json!({"why":"interrupted"})))
        }
        ("response_item", "function_call" | "custom_tool_call") => {
            let arguments = p
                .get("arguments")
                .or(p.get("input"))
                .unwrap_or(&Value::Null);
            out.push(event(
                base,
                "tool",
                json!({"name":p["name"],"call_id":p["call_id"],"input":input(arguments)}),
            ))
        }
        ("response_item", "function_call_output" | "custom_tool_call_output") => {
            let (s, ok) = output(&p["output"]);
            out.push(event(
                base,
                "tool_done",
                json!({"call_id":p["call_id"],"output":s,"ok":ok}),
            ))
        }
        ("response_item", "message") => {
            let s = content(&p["content"]);
            let role = text(&p["role"]);
            if role == "assistant" && !s.trim().is_empty() {
                out.push(event(base, "reply", json!({"text":cap(&s,4000)})))
            } else if role == "user" && !m.side && !noise(&s, true) {
                out.push(event(base, "user", json!({"text":cap(&s,4000)})))
            }
        }
        _ => {}
    }
}
/// A sub-agent rollout replays its parent's history before its own task: records are
/// held back until the task anchor. Returns whether the record was consumed here.
fn codex_side(m: &mut Meta, v: &Value, base: &Value, out: &mut Vec<Value>) -> bool {
    let p = &v["payload"];
    let record = text(&v["type"]);
    let kind = text(&p["type"]);
    if record == "event_msg" && kind == "task_started" {
        let turn = text(&p["turn_id"]);
        if !m.started && turn.len() == 36 && turn > m.session.as_str() {
            m.start_turn = turn.into();
        }
        return true;
    }
    if let Some(anchor) = side_anchor(m, p, record, kind) {
        let identity = format!(
            "{}:{}:{}",
            v["timestamp"], p["internal_chat_message_metadata_passthrough"]["turn_id"], anchor
        );
        if m.seen.insert(identity) {
            m.started = true;
            out.push(event(
                base,
                "user",
                json!({"text":cap(&anchor,4000),"who":"subagent"}),
            ));
        }
        return true;
    }
    !m.started
}
/// The task or message a sub-agent received, as its visible user turn.
fn side_anchor(m: &Meta, p: &Value, record: &str, kind: &str) -> Option<String> {
    if record != "response_item" {
        return None;
    }
    if kind == "agent_message" && !m.agent_path.is_empty() && p["recipient"] == m.agent_path {
        let visible = content(&p["content"]);
        let visible = if visible.is_empty() {
            text(&p["message"]).to_owned()
        } else {
            visible
        };
        let message_type = visible
            .lines()
            .find_map(|l| l.strip_prefix("Message Type:"))
            .map(str::trim)
            .unwrap_or("");
        if !["NEW_TASK", "MESSAGE", "FOLLOWUP_TASK"].contains(&message_type) {
            return None;
        }
        let body = visible
            .split_once("Payload:")
            .map(|(_, s)| s.trim())
            .unwrap_or(&visible);
        let label = if message_type == "NEW_TASK" {
            "task"
        } else {
            "message"
        };
        return Some(format!("[subagent {label}] {body}"));
    }
    if kind == "message" && p["role"] == "user" && m.agent_path.is_empty() {
        let raw = content(&p["content"]);
        let turn = text(&p["internal_chat_message_metadata_passthrough"]["turn_id"]);
        let eligible = m.started
            || (!m.start_turn.is_empty()
                && if turn.is_empty() {
                    !m.replayed
                } else {
                    turn == m.start_turn
                });
        if eligible && !noise(&raw, true) {
            let label = if m.started { "message" } else { "task" };
            return Some(format!("[subagent {label}] {raw}"));
        }
    }
    None
}
fn antigravity(m: &mut Meta, v: &Value, base: &Value, out: &mut Vec<Value>) {
    match (text(&v["type"]), text(&v["source"])) {
        ("USER_INPUT", "USER_EXPLICIT") => {
            let s = text(&v["content"]);
            let s = s
                .split_once("<USER_REQUEST>")
                .map(|(_, t)| t.split("</USER_REQUEST>").next().unwrap_or(t))
                .unwrap_or(s);
            if !s.trim().is_empty() {
                out.push(event(base, "user", json!({"text":cap(s,4000)})))
            }
        }
        ("PLANNER_RESPONSE", "MODEL") => {
            for call in v["tool_calls"].as_array().into_iter().flatten() {
                let args = &call["args"];
                let input = ["CommandLine", "AbsolutePath", "Query", "toolSummary"]
                    .iter()
                    .find_map(|k| args[k].as_str())
                    .unwrap_or("");
                m.announced
                    .push_back((text(&call["name"]).into(), cap(input, 400)));
            }
            while m.announced.len() > 50 {
                m.announced.pop_front();
            }
            if !text(&v["content"]).trim().is_empty() {
                out.push(event(
                    base,
                    "reply",
                    json!({"text":cap(text(&v["content"]),4000)}),
                ))
            }
            if v["tool_calls"].as_array().is_none_or(|a| a.is_empty()) {
                out.push(event(base, "done", json!({})))
            }
        }
        (
            "RUN_COMMAND" | "VIEW_FILE" | "GREP_SEARCH" | "CODE_ACTION" | "LIST_DIRECTORY"
            | "GENERIC",
            "MODEL",
        ) => {
            let (name, input) = m
                .announced
                .pop_front()
                .unwrap_or_else(|| (text(&v["type"]).to_lowercase(), String::new()));
            out.push(event(
                base,
                "tool_done",
                json!({"name":name,"input":input,"output":cap(text(&v["content"]),400),"ok":match text(&v["status"]){"DONE"=>Some(true),"ERROR"=>Some(false),_=>None}}),
            ));
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recorded_journals_and_lifecycle() {
        let mut m = Meta {
            session: "root".into(),
            ..Meta::default()
        };
        let e = parse(
            "claude",
            &mut m,
            &json!({"type":"assistant","sessionId":"root","cwd":"/work/project","timestamp":"2026-01-01T00:00:00Z","message":{"model":"opus","content":[{"type":"tool_use","id":"t","name":"Bash","input":{"command":"ls"}},{"type":"text","text":"done"}],"stop_reason":"end_turn"}}),
        );
        assert_eq!(
            e.iter().map(|e| text(&e["type"])).collect::<Vec<_>>(),
            ["tool", "reply", "done"]
        );
        assert_eq!(e[0]["project"], "project");
        assert_eq!(e[0]["input"], "ls");
        let mut child = Meta {
            session: "agent-child".into(),
            side: true,
            ..Meta::default()
        };
        let e = parse(
            "claude",
            &mut child,
            &json!({"type":"user","sessionId":"root","message":{"content":"child work"}}),
        );
        assert_eq!(e[0]["session"], "agent-child");
        assert_eq!(e[0]["parent"], "root");
        assert!(parse(
            "claude",
            &mut m,
            &json!({"type":"user","isSidechain":true,"message":{"content":"duplicate"}})
        )
        .is_empty());
        assert!(parse("codex",&mut m,&json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"# AGENTS.md instructions\nnoise"}]}})).is_empty());
        let e = parse(
            "codex",
            &mut m,
            &json!({"type":"event_msg","payload":{"type":"turn_aborted"}}),
        );
        assert_eq!(e[0]["why"], "interrupted");
        let e = parse(
            "pi",
            &mut m,
            &json!({"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"hello"}],"stopReason":"stop"}}),
        );
        assert_eq!(e.len(), 2);
        assert_eq!(e[1]["type"], "done");
        let e = parse(
            "antigravity",
            &mut m,
            &json!({"type":"USER_INPUT","source":"USER_EXPLICIT","content":"prefix<USER_REQUEST>hello</USER_REQUEST>suffix"}),
        );
        assert_eq!(e[0]["text"], "hello");
    }
    #[test]
    fn pi_header_and_subagent_metadata() {
        let mut m = Meta::default();
        m.header(
            "pi",
            &json!({"type":"session","id":"child","parentSessionId":"parent","cwd":"/work/repo"}),
        );
        m.header("pi", &json!({"type":"model_change","modelId":"model"}));
        m.header("pi", &json!({"type":"title","title":"a task"}));
        assert_eq!(m.session, "child");
        assert!(m.side);
        assert_eq!(m.title, "a task");
        assert_eq!(m.model, "model");
    }
}

#[cfg(test)]
mod fork_tests {
    use super::*;
    #[test]
    fn copied_parent_stays_out_of_child_until_task_anchor() {
        let mut m = Meta::default();
        parse(
            "codex",
            &mut m,
            &json!({"type":"session_meta","payload":{"id":"01990000-0000-7000-8000-000000000001","thread_source":"subagent","parent_thread_id":"parent","agent_path":"/root/child"}}),
        );
        assert!(parse("codex",&mut m,&json!({"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"copied parent reply"}]}})).is_empty());
        let e = parse(
            "codex",
            &mut m,
            &json!({"type":"response_item","payload":{"type":"agent_message","recipient":"/root/child","content":[{"type":"text","text":"Message Type: NEW_TASK\nTask name: /root/child\nPayload:\nreview code"}]}}),
        );
        assert_eq!(e[0]["text"], "[subagent task] review code");
        assert_eq!(e[0]["parent"], "parent");
        let e = parse(
            "codex",
            &mut m,
            &json!({"type":"event_msg","payload":{"type":"task_complete"}}),
        );
        assert_eq!(e[0]["type"], "done");
    }
}

#[cfg(test)]
mod native_metadata_tests {
    use super::*;
    #[test]
    fn pi_paths_and_antigravity_call_names() {
        let m=Meta::from_path("pi",Path::new("/home/.pi/agent/sessions/--work-demo--/2026-01-01_root-parent/2026-01-01_child.jsonl"));
        assert!(m.side);
        assert_eq!(m.parent, "root-parent");
        assert_eq!(m.project, "work/demo");
        let mut m = Meta {
            session: "root".into(),
            ..Meta::default()
        };
        parse(
            "antigravity",
            &mut m,
            &json!({"type":"PLANNER_RESPONSE","source":"MODEL","tool_calls":[{"name":"run_command","args":{"CommandLine":"cargo test"}}]}),
        );
        let e = parse(
            "antigravity",
            &mut m,
            &json!({"type":"RUN_COMMAND","source":"MODEL","status":"DONE","content":"ok"}),
        );
        assert_eq!(e[0]["name"], "run_command");
        assert_eq!(e[0]["input"], "cargo test");
    }
}

#[cfg(test)]
mod shared_noise_tests {
    use super::*;
    #[test]
    fn live_rejects_every_ingest_wrapper() {
        for text in [
            "<command-args>test",
            "<user-prompt-submit-hook>hook",
            "prefix <command-name>x</command-name>",
            r#"{"type":"idle_notification"}"#,
        ] {
            assert!(noise(text, false), "{text}");
        }
        for text in [
            "<INSTRUCTIONS>rules",
            "<user_instructions>rules",
            "<subagent_notification>done",
            "<goal_context>x",
            "<codex_internal_context>x",
            "<codex_delegation>x",
            "<realtime_delegation>x",
            "<user_action>x",
            "<image>x",
        ] {
            assert!(noise(text, true), "{text}");
        }
        assert!(!noise("please fix the build", true));
    }
}
