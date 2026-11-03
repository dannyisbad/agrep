//! Bounded JSON presentation of the same history and evidence read by the CLI.
use crate::{evidence, history};
use anyhow::Result;
use rusqlite::Connection;
use serde_json::{json, Value};
use std::collections::BTreeMap;
fn bounded(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
pub fn chats(c: &Connection, limit: usize, side: bool) -> Result<Value> {
    let sessions: Vec<_> = history::sessions(c)?
        .into_iter()
        .filter(|s| side || !s.side)
        .collect();
    let total = sessions.len();
    let mut chats = vec![];
    for s in sessions.into_iter().take(limit) {
        let latest = history::latest(c, &s)?;
        let count = history::turn_count(c, &s)?;
        chats.push(json!({"session":s.id,"agent":s.agent,"project":bounded(&s.project,1024),"title":bounded(&s.title,256),"first_text":bounded(&s.title,500),"first_text_truncated":s.title.chars().count()>500,"last_text":latest.map(|r| bounded(&r.text,500)),"n_msgs":count,"first_ts":s.first_ts,"last_ts":s.last_ts,"side":s.side}));
    }
    Ok(json!({"total":total,"totals_exact":true,"chats":chats}))
}
pub fn window(c: &Connection, session: &str, turn: i64, radius: i64) -> Result<Option<Value>> {
    let request = evidence::Request::new(session, Some(turn), Some(radius.clamp(0, 12)));
    let w = match evidence::load(c, &request) {
        Ok(w) => w,
        Err(e) if e.to_string().starts_with("no indexed session matches") => return Ok(None),
        Err(e) => return Err(e),
    };
    let n_msgs = c.query_row(
        "SELECT COUNT(DISTINCT position) FROM prose WHERE session=? AND visible=1",
        [&w.session],
        |r| r.get::<_, i64>(0),
    )?;
    let mut turns = BTreeMap::<i64, Value>::new();
    let mut budget = 192000usize;
    let selected: std::collections::BTreeSet<_> = w.rows.iter().map(|r| r.turn).collect();
    for row in &w.rows {
        if budget == 0 && !turns.contains_key(&row.turn) {
            continue;
        }
        let value = turns.entry(row.turn).or_insert_with(|| json!({"turn":row.turn,"ts":row.ts,"who":row.who,"text":"","reply":"","text_truncated":false,"reply_truncated":false}));
        let field = if row.who == "agent" { "reply" } else { "text" };
        let retained = bounded(&row.text, 32000.min(budget));
        let count = retained.chars().count();
        budget -= count;
        let old = value[field].as_str().unwrap_or("");
        value[field] = json!(if old.is_empty() {
            retained
        } else {
            format!("{old}\n{retained}")
        });
        value[format!("{field}_truncated")] =
            json!(value[format!("{field}_truncated")] == true || row.source_chars > count);
    }
    let mut event_budget = 48000usize;
    let mut events = vec![];
    for e in &w.events {
        if events.len() >= 96 || event_budget == 0 {
            break;
        }
        let input = bounded(&e.input, 8000.min(event_budget));
        event_budget -= input.chars().count();
        events.push(json!({"turn":e.turn,"ts":e.ts,"kind":e.kind,"name":e.name,"input_truncated":e.input_chars>input.chars().count(),"input":input,"ok":e.ok}));
    }
    let omitted_turns = selected.len().saturating_sub(turns.len());
    let omitted_events = w.events.len() - events.len();
    Ok(Some(
        json!({"session":w.session,"agent":w.agent,"project":w.project,"title":w.title,"n_msgs":n_msgs,"first_turn":w.first,"last_turn":w.last,"center":w.center,"turns":turns.into_values().collect::<Vec<_>>(),"events":events,"truncated":budget==0||event_budget==0||omitted_turns>0||omitted_events>0,"omitted_turns":omitted_turns,"omitted_events":omitted_events}),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{data::DataRoot, store};
    #[test]
    fn window_uses_canonical_event_turns_and_prefixes() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path(), true).unwrap();
        let c = store::writer(&root).unwrap();
        c.execute_batch("INSERT INTO sources VALUES('fixture','pi',0,0,'',NULL); INSERT INTO sessions VALUES('pi','root-long','demo','','','root-long',0,'title',0,99); INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position,visible) VALUES(1,'pi','root-long','demo',0,'user','','',1,X'6869','','',0,1); INSERT INTO event_sessions VALUES(1,'pi','root-long'); INSERT INTO symbols(id,value) VALUES(1,'tool'),(2,'shell'); INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,input_end,project_key) VALUES(1,1,1,1,'call',1,2,99,0,0,4,0,0,'','','',11,0);").unwrap();
        c.execute(
            "INSERT INTO tool_blobs(id,digest,body) VALUES(1,X'01',?)",
            [store::encode_tool("shell: test").unwrap()],
        )
        .unwrap();
        c.execute("UPDATE event_rows SET blob=1 WHERE id=1", [])
            .unwrap();
        let value = window(&c, "root-", 0, 0).unwrap().unwrap();
        assert_eq!(value["session"], "root-long");
        assert_eq!(value["events"][0]["ts"], 99);
        assert_eq!(value["events"][0]["turn"], 0);
        assert_eq!(value["events"][0]["input"], "test");
        assert!(value.get("events_unavailable").is_none());
    }
}
