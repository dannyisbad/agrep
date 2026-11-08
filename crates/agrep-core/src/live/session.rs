//! How one observed event changes its session's state.
use super::{cap, text, Session};
use serde_json::{json, Value};

impl Session {
    /// Folds the event into this session and annotates it with the resulting state.
    pub(super) fn apply(&mut self, e: &mut Value, kind: &str, ts: i64) {
        self.adopt(e);
        self.sub |= e["sub_session"].as_bool().unwrap_or(false) || !self.parent.is_empty();
        self.last_ts = self.last_ts.max(ts);
        if kind == "user" {
            self.turn_n += 1;
        }
        if matches!(kind, "user" | "reply") && !text(&e["text"]).trim().is_empty() {
            self.last_text = cap(text(&e["text"]), 1000);
        }
        self.track_tools(e, kind, ts);
        self.advance(e, kind);
        if matches!(kind, "user" | "reply" | "tool" | "tool_done") {
            self.working = true;
            self.state_ts = ts;
        }
        if kind == "done" {
            self.working = false;
            self.state_ts = ts;
        }
        self.annotate(e, kind);
    }
    fn adopt(&mut self, e: &Value) {
        for field in ["project", "title", "model", "parent"] {
            let v = text(&e[field]);
            if v.is_empty() {
                continue;
            }
            match field {
                "project" => self.project = v.into(),
                "title" => self.title = v.into(),
                "model" => self.model = v.into(),
                "parent" => self.parent = v.into(),
                _ => {}
            }
        }
    }
    /// Open tool calls; a finished call learns its name and duration from its start.
    fn track_tools(&mut self, e: &mut Value, kind: &str, ts: i64) {
        match kind {
            "tool" => {
                self.pending
                    .insert(text(&e["call_id"]).into(), (text(&e["name"]).into(), ts));
            }
            "tool_done" => {
                if let Some((name, start)) = self.pending.remove(text(&e["call_id"])) {
                    if text(&e["name"]).is_empty() {
                        e["name"] = json!(name)
                    }
                    e["dur"] = json!((ts - start).max(0));
                }
            }
            "user" | "done" => self.pending.clear(),
            _ => {}
        }
    }
    fn advance(&mut self, e: &Value, kind: &str) {
        match kind {
            "user" => self.state = "thinking".into(),
            "reply" => self.state = "replying".into(),
            "tool" | "tool_done" => self.state = self.tool_state(),
            "done" => {
                self.state = e["why"].as_str().unwrap_or("done").into();
                self.writer = false;
            }
            "queued" => {
                if text(&e["op"]) == "enqueue" {
                    self.queued += 1;
                    self.queued_text = text(&e["text"]).into()
                } else {
                    self.queued = self.queued.saturating_sub(1);
                    if self.queued == 0 {
                        self.queued_text.clear()
                    }
                }
            }
            _ => {}
        }
    }
    /// The latest open tool call, with how many others are open.
    fn tool_state(&self) -> String {
        self.pending
            .values()
            .next_back()
            .map(|(name, _)| {
                format!(
                    "⚙ {name}{}",
                    if self.pending.len() > 1 {
                        format!(" +{}", self.pending.len() - 1)
                    } else {
                        String::new()
                    }
                )
            })
            .unwrap_or_else(|| "thinking".into())
    }
    fn annotate(&self, e: &mut Value, kind: &str) {
        e["tn"] = json!(self.turn_n);
        e["sstate"] = json!(self.state);
        e["sworking"] = json!(self.working);
        e["sts"] = json!(self.state_ts);
        if !self.model.is_empty() {
            e["smodel"] = json!(self.model)
        }
        if kind == "queued" {
            e["squeued"] = json!(self.queued);
            e["squeued_text"] = json!(self.queued_text)
        }
        if self.sub && kind == "user" {
            e["who"] = json!("subagent")
        }
    }
}
