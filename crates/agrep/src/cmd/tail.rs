use agrep_core::{
    live::{self, Watcher},
    terminal::Signals,
};
use anyhow::{ensure, Result};
use serde_json::{json, Value};
use std::{
    io::{self, Write},
    time::Duration,
};
fn line(v: &Value) -> Result<()> {
    let mut out = io::stdout().lock();
    agrep_core::json_output::to_writer(&mut out, v)?;
    writeln!(out)?;
    out.flush()?;
    Ok(())
}
pub fn run(
    agent: Vec<String>,
    session: Option<String>,
    events: Option<String>,
    snapshot: bool,
) -> Result<i32> {
    match follow(agent, session, events, snapshot) {
        Err(e) if agrep_core::terminal::output_closed(&e) => Ok(0),
        other => other,
    }
}
fn follow(
    agent: Vec<String>,
    session: Option<String>,
    events: Option<String>,
    snapshot: bool,
) -> Result<i32> {
    let mut agents = vec![];
    for a in &agent {
        for a in a.split(',').filter(|s| !s.trim().is_empty()) {
            agents.push(live::normalize_agent(a)?);
        }
    }
    ensure!(
        agent.is_empty() || !agents.is_empty(),
        "--agent requires at least one supported agent name"
    );
    agents.sort();
    agents.dedup();
    let session = session
        .as_deref()
        .map(live::normalize_session)
        .transpose()?;
    let event_list = events.as_deref().unwrap_or("done").trim().to_lowercase();
    let mut types: Vec<_> = event_list
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect();
    types.sort();
    types.dedup();
    ensure!(!types.is_empty(), "--events requires an event type or all");
    ensure!(
        !types.iter().any(|s| s == "all") || types.len() == 1,
        "all must be the only --events value"
    );
    ensure!(
        types
            .iter()
            .all(|s| s == "all" || live::EVENT_TYPES.contains(&s.as_str())),
        "unsupported event type"
    );
    let signals = Signals::new()?;
    if agrep_core::terminal::output_is_closed() {
        return Ok(0);
    }
    let mut watcher = Watcher::new();
    watcher.poll(&mut |_| Ok(()))?;
    if snapshot {
        let mut value = watcher.snapshot().scoped(&agents, session.as_deref());
        value["type"] = json!("snapshot");
        line(&value)?;
        return Ok(0);
    }
    line(
        &json!({"type":"tail_ready","agents":if agents.is_empty(){json!("all")}else{json!(agents)},"events":if types==["all"]{json!("all")}else{json!(types)}}),
    )?;
    loop {
        if agrep_core::terminal::output_is_closed() {
            return Ok(0);
        }
        if signals.pending() != 0 {
            return Ok(signals.exit_code());
        }
        watcher.poll(&mut |e| {
            if (agents.is_empty()
                || agents
                    .iter()
                    .any(|s| Some(s.as_str()) == e["agent"].as_str()))
                && (types == ["all"]
                    || types.iter().any(|s| Some(s.as_str()) == e["type"].as_str()))
                && session
                    .as_ref()
                    .is_none_or(|s| e["session"].as_str().is_some_and(|id| id.contains(s)))
            {
                line(e)?;
            }
            Ok(())
        })?;
        for _ in 0..14 {
            if signals.pending() != 0 {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}
