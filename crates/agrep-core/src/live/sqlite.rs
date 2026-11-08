//! Native store polling. Mutable rows are rechecked even when updated clocks tie.
use super::{cap, now, stamp, text, Source, Watcher, WINDOW_MS};
use anyhow::{bail, Result};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{json, Value};
use std::{collections::HashMap, path::Path, time::Duration};

fn has(c: &Connection, table: &str) -> Result<bool> {
    Ok(c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
        [table],
        |r| r.get(0),
    )?)
}
fn json_value(row: &rusqlite::Row<'_>, n: usize) -> rusqlite::Result<Value> {
    let bytes = match row.get_ref(n)? {
        rusqlite::types::ValueRef::Text(v) | rusqlite::types::ValueRef::Blob(v) => v,
        _ => return Ok(Value::Null),
    };
    serde_json::from_slice(bytes).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(n, rusqlite::types::Type::Text, Box::new(e))
    })
}
/// Where a polled event goes: the source's dedupe state, the watcher, and the caller.
struct Emit<'a, F> {
    source: &'a mut Source,
    w: &'a mut Watcher,
    sink: &'a mut F,
}
impl<F: FnMut(&Value) -> Result<()>> Emit<'_, F> {
    fn send(&mut self, key: &str, mut e: Value) -> Result<()> {
        let signature = e.to_string();
        if self.source.seen.get(key) == Some(&signature) {
            return Ok(());
        }
        // Mutable text keeps its own signature; tool updates dedupe by phase, not output bytes.
        e["ts"] = e["ts"]
            .as_i64()
            .filter(|t| *t > 0)
            .map_or_else(|| json!(now()), |t| json!(t));
        self.w.emit(e, &mut *self.sink)?;
        self.source.seen.insert(key.into(), signature);
        Ok(())
    }
    fn phase(&mut self, key: &str, base: &Value, p: &Value, v2: bool) -> Result<()> {
        let state = &p["state"];
        let status = text(&state["status"]);
        let running = matches!(status, "running" | "pending");
        let stage = if running { "running" } else { status };
        let signature = format!("{key}:phase:{stage}");
        if self.source.seen.contains_key(&signature) {
            return Ok(());
        }
        let mut e = base.clone();
        e["type"] = json!(if running { "tool" } else { "tool_done" });
        e["name"] = if v2 {
            p["name"].clone()
        } else {
            p["tool"].clone()
        };
        e["call_id"] = json!(key);
        e["input"] = json!(crate::ingest::summarize_tool_input(&state["input"]));
        e["output"] = json!(cap(
            state["output"]
                .as_str()
                .or(state["output"]["output"].as_str())
                .unwrap_or(""),
            400
        ));
        e["ok"] = json!(match status {
            "completed" => Some(true),
            "error" => Some(false),
            _ => None,
        });
        self.send(&signature, e)
    }
}
pub(super) fn poll(
    agent: &str,
    path: &Path,
    source: &mut Source,
    w: &mut Watcher,
    sink: &mut impl FnMut(&Value) -> Result<()>,
) -> Result<()> {
    let c = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    c.busy_timeout(Duration::from_millis(250))?;
    c.execute_batch("PRAGMA query_only=ON; BEGIN")?;
    match agent {
        "opencode" => opencode(&c, source, w, sink),
        "cursor" => cursor(&c, source, w, sink),
        _ => Ok(()),
    }
}
fn opencode(
    c: &Connection,
    source: &mut Source,
    w: &mut Watcher,
    sink: &mut impl FnMut(&Value) -> Result<()>,
) -> Result<()> {
    let mut out = Emit { source, w, sink };
    if has(c, "session_message")? {
        opencode_v2(c, &mut out)
    } else if has(c, "part")? && has(c, "message")? {
        opencode_v1(c, &mut out)
    } else {
        bail!("unrecognized OpenCode live schema")
    }
}
/// Rows to read: those updated since the floor (`?1`) and the still-unfinished ones
/// (the returned placeholder list), with the store's current `max_sql` watermark.
fn watermark(
    c: &Connection,
    source: &mut Source,
    max_sql: &str,
) -> Result<(i64, Vec<rusqlite::types::Value>, String)> {
    let max: i64 = c.query_row(max_sql, [], |r| r.get(0))?;
    if source.floor == 0 {
        source.floor = (max - 120_000).max(0)
    }
    let mut parameters = vec![rusqlite::types::Value::Integer(source.floor)];
    let mut pending = Vec::new();
    for id in &source.unfinished {
        parameters.push(id.clone().into());
        pending.push(format!("?{}", parameters.len()));
    }
    let pending = if pending.is_empty() {
        "NULL".into()
    } else {
        pending.join(",")
    };
    Ok((max, parameters, pending))
}
fn track_unfinished(source: &mut Source, id: &str, role: &str, data: &Value) {
    if role == "assistant" && data["time"]["completed"].is_null() {
        source.unfinished.insert(id.to_owned());
    } else {
        source.unfinished.remove(id);
    }
}
fn completed(data: &Value, role: &str) -> Option<i64> {
    data["time"]["completed"]
        .as_i64()
        .filter(|_| role == "assistant")
}
fn opencode_v2<F: FnMut(&Value) -> Result<()>>(
    c: &Connection,
    out: &mut Emit<'_, F>,
) -> Result<()> {
    let (max, parameters, pending) = watermark(
        c,
        out.source,
        "SELECT COALESCE(MAX(time_updated),0) FROM session_message",
    )?;
    let sql=format!("SELECT m.id,m.session_id,m.type,m.time_updated,m.data,COALESCE(s.directory,''),COALESCE(s.title,''),COALESCE(s.parent_id,''),COALESCE(s.model,'') FROM session_message m LEFT JOIN session_v2 s ON s.id=m.session_id WHERE m.time_updated>=?1 OR m.id IN ({pending}) ORDER BY m.time_updated,m.id");
    let mut stmt = c.prepare(&sql)?;
    for row in stmt.query_map(rusqlite::params_from_iter(&parameters), |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, i64>(3)?,
            json_value(r, 4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
            r.get::<_, String>(7)?,
            r.get::<_, String>(8)?,
        ))
    })? {
        let (id, session, role, ts, data, cwd, title, parent, model) = row?;
        track_unfinished(out.source, &id, &role, &data);
        let base = json!({"agent":"opencode","session":session,"ts":ts,"project":crate::ingest::project_name(&cwd),"title":title,"parent":parent,"model":data["modelID"].as_str().unwrap_or(&model)});
        opencode_v2_message(&id, &role, &data, base, out)?;
    }
    out.source.floor = out.source.floor.max(max.saturating_sub(120_000));
    Ok(())
}
fn opencode_v2_message<F: FnMut(&Value) -> Result<()>>(
    id: &str,
    role: &str,
    data: &Value,
    mut base: Value,
    out: &mut Emit<'_, F>,
) -> Result<()> {
    let body = if role == "user" {
        let raw = text(&data["text"]);
        if crate::ingest::opencode::is_injected_part(raw) {
            String::new()
        } else {
            raw.to_owned()
        }
    } else {
        data["content"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter(|p| p["type"] == "text")
                    .filter_map(|p| p["text"].as_str())
                    .collect::<Vec<_>>()
                    .join("\n")
            })
            .unwrap_or_default()
    };
    // The tool phases and the completion below start from this event, text included.
    if !body.trim().is_empty() && matches!(role, "user" | "assistant") {
        base["type"] = json!(if role == "user" { "user" } else { "reply" });
        base["text"] = json!(cap(&body, 4000));
        out.send(&format!("{id}:text"), base.clone())?;
    }
    if let Some(parts) = data["content"].as_array() {
        for (i, p) in parts
            .iter()
            .enumerate()
            .filter(|(_, p)| p["type"] == "tool")
        {
            let key = p["id"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| format!("{id}:{i}"));
            out.phase(&key, &base, p, true)?;
        }
    }
    if let Some(ts) = completed(data, role) {
        base["type"] = json!("done");
        base["ts"] = json!(ts);
        base.as_object_mut().unwrap().remove("text");
        out.send(&format!("{id}:done"), base)?;
    }
    Ok(())
}
fn opencode_v1<F: FnMut(&Value) -> Result<()>>(
    c: &Connection,
    out: &mut Emit<'_, F>,
) -> Result<()> {
    let (max, parameters, pending) = watermark(
        c,
        out.source,
        "SELECT COALESCE(MAX(time_updated),0) FROM message",
    )?;
    let sql=format!("SELECT m.id,m.session_id,m.time_updated,m.data,COALESCE(s.directory,''),COALESCE(s.title,''),COALESCE(s.parent_id,'') FROM message m LEFT JOIN session s ON s.id=m.session_id WHERE m.time_updated>=?1 OR m.id IN (SELECT message_id FROM part WHERE time_updated>=?1) OR m.id IN ({pending}) ORDER BY m.time_updated,m.id");
    let mut stmt = c.prepare(&sql)?;
    for row in stmt.query_map(rusqlite::params_from_iter(&parameters), |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, i64>(2)?,
            json_value(r, 3)?,
            r.get::<_, String>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
        ))
    })? {
        let (id, session, ts, data, cwd, title, parent) = row?;
        let role = text(&data["role"]);
        track_unfinished(out.source, &id, role, &data);
        let base = json!({"agent":"opencode","session":session,"ts":ts,"project":crate::ingest::project_name(&cwd),"title":title,"parent":parent,"model":data["modelID"].as_str().unwrap_or("")});
        let mut parts =
            c.prepare("SELECT id,data FROM part WHERE message_id=? ORDER BY time_created,id")?;
        let parts: Vec<_> = parts
            .query_map([&id], |r| Ok((r.get::<_, String>(0)?, json_value(r, 1)?)))?
            .collect::<Result<_, _>>()?;
        opencode_v1_message(&id, role, &data, &base, &parts, out)?;
    }
    out.source.floor = out.source.floor.max(max.saturating_sub(120_000));
    Ok(())
}
fn opencode_v1_message<F: FnMut(&Value) -> Result<()>>(
    id: &str,
    role: &str,
    data: &Value,
    base: &Value,
    parts: &[(String, Value)],
    out: &mut Emit<'_, F>,
) -> Result<()> {
    let body = parts
        .iter()
        .filter(|(_, p)| p["type"] == "text")
        .filter_map(|(_, p)| p["text"].as_str())
        .filter(|text| role != "user" || !crate::ingest::opencode::is_injected_part(text))
        .collect::<Vec<_>>()
        .join("\n");
    if !body.trim().is_empty() && matches!(role, "user" | "assistant") {
        let mut e = base.clone();
        e["type"] = json!(if role == "user" { "user" } else { "reply" });
        e["text"] = json!(cap(&body, 4000));
        out.send(&format!("{id}:text"), e)?;
    }
    for (pid, p) in parts.iter().filter(|(_, p)| p["type"] == "tool") {
        out.phase(p["callID"].as_str().unwrap_or(pid), base, p, false)?;
    }
    if let Some(ts) = completed(data, role) {
        let mut e = base.clone();
        e["type"] = json!("done");
        e["ts"] = json!(ts);
        out.send(&format!("{id}:done"), e)?;
    }
    Ok(())
}
fn cursor(
    c: &Connection,
    source: &mut Source,
    w: &mut Watcher,
    sink: &mut impl FnMut(&Value) -> Result<()>,
) -> Result<()> {
    if !has(c, "cursorDiskKV")? {
        return Ok(());
    }
    let composers = cursor_composers(c, source)?;
    let mut out = Emit { source, w, sink };
    for (id, meta) in composers {
        cursor_composer(c, &id, meta, &mut out)?;
    }
    Ok(())
}
/// Composers updated within the window, plus those already tracked. Parsed metadata is
/// cached by content signature.
fn cursor_composers(c: &Connection, source: &mut Source) -> Result<HashMap<String, Value>> {
    let mut stmt =
        c.prepare("SELECT key,value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")?;
    let mut composers = HashMap::new();
    for row in stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
        let (key, raw) = row?;
        let signature = blake2b_simd::blake2b(raw.as_bytes()).to_hex().to_string();
        if source
            .metadata
            .get(&key)
            .is_none_or(|(old, _)| old != &signature)
        {
            let data: Value = serde_json::from_str(&raw)?;
            let old = now() - data["lastUpdatedAt"].as_i64().unwrap_or(0) > WINDOW_MS;
            let tracked = source.seen.contains_key(&format!(
                "{}:tracked",
                key.trim_start_matches("composerData:")
            ));
            source.metadata.insert(
                key.clone(),
                (signature, if old && !tracked { Value::Null } else { data }),
            );
        }
        let data = source.metadata[&key].1.clone();
        if data.is_null() {
            continue;
        }
        let id = key.trim_start_matches("composerData:").to_owned();
        let ts = data["lastUpdatedAt"].as_i64().unwrap_or(0);
        if now() - ts > WINDOW_MS && !source.seen.contains_key(&format!("{id}:tracked")) {
            continue;
        }
        source.seen.insert(format!("{id}:tracked"), String::new());
        composers.insert(id, data);
    }
    Ok(composers)
}
fn cursor_composer<F: FnMut(&Value) -> Result<()>>(
    c: &Connection,
    id: &str,
    meta: Value,
    out: &mut Emit<'_, F>,
) -> Result<()> {
    let cwd = text(&meta["workspaceIdentifier"]["uri"]["fsPath"]);
    let base = json!({"agent":"cursor","session":id,"project":crate::ingest::project_name(cwd),"title":meta["name"].as_str().unwrap_or(""),"model":meta["modelConfig"]["modelName"].as_str().unwrap_or("")});
    for header in meta["fullConversationHeadersOnly"]
        .as_array()
        .into_iter()
        .flatten()
    {
        let bid = text(&header["bubbleId"]);
        if bid.is_empty() {
            continue;
        }
        let key = format!("bubbleId:{id}:{bid}");
        let bubble = c
            .query_row("SELECT value FROM cursorDiskKV WHERE key=?", [&key], |r| {
                json_value(r, 0)
            })
            .optional()?;
        if let Some(b) = bubble {
            cursor_bubble(&key, bid, &b, &base, out)?;
        }
    }
    if meta["generatingBubbleIds"]
        .as_array()
        .is_none_or(|a| a.is_empty())
    {
        let mut e = base;
        e["type"] = json!("done");
        e["ts"] = meta["lastUpdatedAt"].clone();
        if meta["status"] == "aborted" {
            e["why"] = json!("interrupted")
        }
        out.send(&format!("{id}:done"), e)?;
    }
    Ok(())
}
fn cursor_bubble<F: FnMut(&Value) -> Result<()>>(
    key: &str,
    bid: &str,
    b: &Value,
    base: &Value,
    out: &mut Emit<'_, F>,
) -> Result<()> {
    let mut e = base.clone();
    e["ts"] = json!(stamp(&b["createdAt"]));
    if b["toolFormerData"].is_object() {
        let p = &b["toolFormerData"];
        let status = text(&p["status"]);
        let terminal = matches!(
            status,
            "completed"
                | "error"
                | "errored"
                | "failed"
                | "cancelled"
                | "canceled"
                | "rejected"
                | "aborted"
        );
        e["type"] = json!("tool");
        e["name"] = p["name"].clone();
        e["input"] = json!(cap(text(&p["params"]), 400));
        e["call_id"] = json!(p["toolCallId"].as_str().unwrap_or(bid));
        out.send(&format!("{key}:tool"), e.clone())?;
        if terminal {
            e["type"] = json!("tool_done");
            e["output"] = json!(cap(text(&p["result"]), 400));
            e["ok"] = json!(status == "completed");
            out.send(&format!("{key}:done"), e)?;
        }
    } else if (b["type"] == 1 || b["type"] == 2)
        && b["isThought"] != true
        && !text(&b["text"]).trim().is_empty()
    {
        e["type"] = json!(if b["type"] == 1 { "user" } else { "reply" });
        e["text"] = json!(cap(text(&b["text"]), 4000));
        out.send(key, e)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn opencode_mutable_rows_complete_at_equal_timestamps() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT,title TEXT,parent_id TEXT);CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,time_created INT,time_updated INT,data TEXT);CREATE TABLE part(id TEXT PRIMARY KEY,message_id TEXT,time_created INT,time_updated INT,data TEXT);INSERT INTO session VALUES('ses_root','/work/repo','title',NULL);INSERT INTO message VALUES('m','ses_root',100,100,'{\"role\":\"assistant\"}');INSERT INTO part VALUES('p','m',100,100,'{\"type\":\"tool\",\"tool\":\"shell\",\"callID\":\"t\",\"state\":{\"status\":\"running\"}}');").unwrap();
        let mut s = Source::default();
        let mut w = Watcher::new();
        w.booting = false;
        let mut got = vec![];
        opencode(&c, &mut s, &mut w, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0]["type"], "tool");
        c.execute("UPDATE part SET data=?",[r#"{"type":"tool","tool":"shell","callID":"t","state":{"status":"completed","output":"ok"}}"#]).unwrap();
        c.execute(
            "UPDATE message SET data=?",
            [r#"{"role":"assistant","time":{"completed":101}}"#],
        )
        .unwrap();
        opencode(&c, &mut s, &mut w, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 3);
        assert_eq!(got[1]["type"], "tool_done");
        assert_eq!(got[2]["type"], "done");
        opencode(&c, &mut s, &mut w, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 3);
    }
    #[test]
    fn cursor_membership_and_mutable_text() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value TEXT)")
            .unwrap();
        c.execute("INSERT INTO cursorDiskKV VALUES(?1,?2)",rusqlite::params!["composerData:root",json!({"lastUpdatedAt":now(),"generatingBubbleIds":["b"],"fullConversationHeadersOnly":[{"bubbleId":"b"}]}).to_string()]).unwrap();
        c.execute(
            "INSERT INTO cursorDiskKV VALUES(?1,?2)",
            rusqlite::params![
                "bubbleId:root:b",
                json!({"type":2,"text":"hello","createdAt":now()}).to_string()
            ],
        )
        .unwrap();
        c.execute(
            "INSERT INTO cursorDiskKV VALUES(?1,?2)",
            rusqlite::params![
                "bubbleId:root:orphan",
                json!({"type":1,"text":"orphan"}).to_string()
            ],
        )
        .unwrap();
        let mut s = Source::default();
        let mut w = Watcher::new();
        w.booting = false;
        let mut got = vec![];
        cursor(&c, &mut s, &mut w, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 1);
        c.execute(
            "UPDATE cursorDiskKV SET value=? WHERE key='bubbleId:root:b'",
            [json!({"type":2,"text":"hello world","createdAt":now()}).to_string()],
        )
        .unwrap();
        cursor(&c, &mut s, &mut w, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[1]["text"], "hello world");
    }
}

#[cfg(test)]
mod v2_tests {
    use super::*;
    #[test]
    fn v2_keeps_unfinished_rows_beyond_recent_watermark() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE session_v2(id TEXT PRIMARY KEY,directory TEXT,title TEXT,parent_id TEXT,model TEXT);CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,time_updated INT,data TEXT);INSERT INTO session_v2 VALUES('root','/work/demo','task','parent','model');INSERT INTO session_message VALUES('m','root','assistant',1,'{\"content\":[{\"type\":\"text\",\"text\":\"hello\"},{\"type\":\"tool\",\"id\":\"t\",\"name\":\"shell\",\"state\":{\"status\":\"running\"}}]}');").unwrap();
        let mut source = Source::default();
        let mut w = Watcher::new();
        w.booting = false;
        let mut got = vec![];
        opencode(&c, &mut source, &mut w, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[0]["parent"], "parent");
        source.floor = 1_000_000;
        c.execute(
            "UPDATE session_message SET data=? WHERE id='m'",
            [r#"{"content":[{"type":"text","text":"finished"}],"time":{"completed":2}}"#],
        )
        .unwrap();
        opencode(&c, &mut source, &mut w, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got[2]["text"], "finished");
        assert_eq!(got[3]["type"], "done");
        assert!(source.unfinished.is_empty());
    }
}

#[cfg(test)]
mod noise_tests {
    use super::*;
    #[test]
    fn opencode_parts_filter_injected_text_before_joining() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(r#"CREATE TABLE session(id TEXT,directory TEXT,title TEXT,parent_id TEXT); CREATE TABLE message(id TEXT,session_id TEXT,time_created INT,time_updated INT,data TEXT); CREATE TABLE part(id TEXT,message_id TEXT,time_created INT,time_updated INT,data TEXT); INSERT INTO session VALUES('root','','',''); INSERT INTO message VALUES('m','root',1,1,'{"role":"user"}');"#).unwrap();
        for (id, text) in [
            ("a", "Called the Read tool"),
            ("b", "actual question"),
            ("c", "<path>attachment"),
        ] {
            c.execute(
                "INSERT INTO part VALUES(?1,'m',1,1,?2)",
                rusqlite::params![id, json!({"type":"text","text":text}).to_string()],
            )
            .unwrap();
        }
        let mut w = Watcher::new();
        w.booting = false;
        let mut events = vec![];
        opencode(&c, &mut Source::default(), &mut w, &mut |e| {
            events.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["text"], "actual question");
    }
}
