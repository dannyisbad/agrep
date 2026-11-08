//! Family activity and delivery of delegated results.
use super::{pending::last_turn, Event};
use crate::history::{self, Row, Session};
use anyhow::Result;
use rusqlite::Connection;
use serde_json::Value;
use std::collections::HashSet;

pub fn latest_activity(rows: &[Row], events: &[Event<'_>]) -> i64 {
    std::iter::once(last_turn(rows).map_or(0, |r| r.ts))
        .chain(rows.iter().filter(|r| r.who == "recap").map(|r| r.ts))
        .chain(
            events
                .iter()
                .filter(|e| {
                    e.kind != "control" || !matches!(e.name, "api_error" | "task_notification")
                })
                .map(|e| e.ts),
        )
        .max()
        .unwrap_or(0)
        .max(0)
}
/// A chat of a family: its session, rows and events.
pub type Member<'a> = (&'a Session, Vec<Row>, Vec<Event<'a>>);
/// Which members of a family (`members[0]` its root) handed their result back to the root:
/// a side chat that yielded it, or whose final reply a reached member's delegation returned.
pub fn handed_back(c: &Connection, members: &[Member<'_>]) -> Result<Vec<bool>> {
    let mut reached = vec![false; members.len()];
    let mut holders = vec![0];
    reached[0] = true;
    for (at, (_, rows, events)) in members.iter().enumerate().skip(1) {
        if yielded(c, rows, events)? {
            reached[at] = true;
            holders.push(at);
        }
    }
    let finals: Vec<String> = members
        .iter()
        .map(|(_, rows, _)| {
            rows.iter()
                .rev()
                .find(|r| r.who == "agent")
                .map(|r| history::one_line(&r.text, usize::MAX))
                .unwrap_or_default()
        })
        .collect();
    while let Some(holder) = holders.pop() {
        let events = &members[holder].2;
        let results: HashSet<&str> = events
            .iter()
            .filter(|e| e.kind == "subagent_result" && e.ok != Some(false))
            .map(|e| e.child_session.as_str())
            .collect();
        // Each delegation call's linked child and flattened output, decoded once a side chat
        // has a final reply long enough to match.
        let mut delegations: Option<Vec<(&str, String)>> = None;
        for side in 1..members.len() {
            if reached[side] {
                continue;
            }
            let id = members[side].0.id.as_str();
            let mut received = results.contains(id);
            if !received && finals[side].len() >= 20 {
                if delegations.is_none() {
                    let mut calls = vec![];
                    for e in events.iter().filter(|e| {
                        matches!(
                            e.name,
                            "task" | "agent" | "subagent" | "wait_agent" | "Task"
                        )
                    }) {
                        calls.push((
                            e.child_session.as_str(),
                            history::one_line(e.output(c)?, usize::MAX),
                        ));
                    }
                    delegations = Some(calls);
                }
                received = delegations.iter().flatten().any(|(child, output)| {
                    (child.is_empty() || *child == id) && output.contains(&finals[side])
                });
            }
            if received {
                reached[side] = true;
                holders.push(side);
            }
        }
    }
    Ok(reached)
}
/// Whether the side chat's last work call is a `yield` that handed its result back.
fn yielded(c: &Connection, rows: &[Row], events: &[Event]) -> Result<bool> {
    let Some(last) = events
        .iter()
        .filter(|e| matches!(e.kind, "tool" | "subagent_result"))
        .max_by_key(|e| e.ts)
    else {
        return Ok(false);
    };
    if !last.name.eq_ignore_ascii_case("yield") || last.ok == Some(false) {
        return Ok(false);
    }
    if rows.iter().any(|r| r.who != "agent" && r.ts > last.ts) {
        return Ok(false);
    }
    Ok(!serde_json::from_str::<Value>(last.input(c)?)
        .ok()
        .is_some_and(|v| v["type"].is_array()))
}
/// Whether a root event linked to the side chat is its result or returned output.
pub fn delivered(c: &Connection, root: &[Event], side: &Session) -> Result<bool> {
    for e in root.iter().filter(|e| e.child_session == side.id) {
        if e.kind == "subagent_result" || !e.output(c)?.is_empty() {
            return Ok(true);
        }
    }
    Ok(false)
}
