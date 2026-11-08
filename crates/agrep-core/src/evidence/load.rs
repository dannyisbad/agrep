//! Load and verify transcript windows in a read snapshot.
use super::{handle_error, Event, Request, Row, Window};
use crate::{
    handles::{self, SessionIndex},
    query::content_digest,
    store,
};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::BTreeSet;

pub fn load(c: &Connection, request: &Request) -> Result<Window> {
    load_in(c, &SessionIndex::load(c)?, request)
}
fn load_in(c: &Connection, index: &SessionIndex, request: &Request) -> Result<Window> {
    let claim = if request.exact {
        Some(handles::parse(&request.target)?)
    } else {
        None
    };
    if claim.is_some() && request.turn.is_some() {
        bail!("a result handle already includes its turn")
    }
    let (target, requested) = if let Some(claim) = &claim {
        (claim.session.clone(), Some(claim.turn))
    } else if request.turn.is_none() {
        match request.target.rsplit_once(':') {
            Some((s, t)) if t.parse::<i64>().is_ok() => (s.to_owned(), Some(t.parse()?)),
            _ => (request.target.clone(), None),
        }
    } else {
        (request.target.clone(), request.turn)
    };
    let session = resolve_session(c, index, &target, claim.as_ref())?;
    let (first, last): (Option<i64>, Option<i64>) = c.query_row(
        "SELECT min(position),max(position) FROM prose WHERE session=? AND visible=1",
        [&session],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let first = first.context("session has no indexed turns")?;
    let last = last.unwrap_or(first);
    let requested = requested.unwrap_or(last);
    let mut center = requested.clamp(first, last);
    let mut notes = Vec::new();
    if center != requested {
        if request.exact {
            return Err(handle_error("stale-result-handle", format!("result handle is stale: turn {requested} is outside chat turns {first}-{last}; rerun the search for a current handle")));
        }
        notes.push(json!({"kind":"turn_clamped","requested":requested,"served":center,"note":format!("turn {requested} clamped to {center} (chat has turns {first}-{last})")}));
    }
    if let Some(claim) = &claim {
        if let Some(digest) = &claim.digest {
            if !has_claim(c, &session, center, digest, claim.event.as_deref())? {
                let radius = request.radius.unwrap_or(last - first).max(4);
                let positions:Vec<i64>=c.prepare("SELECT DISTINCT position FROM prose WHERE session=? AND visible=1 AND position BETWEEN ? AND ? ORDER BY position")?.query_map(params![session,center.saturating_sub(radius),center.saturating_add(radius)],|r|r.get(0))?.collect::<rusqlite::Result<_>>()?;
                let mut holders = Vec::new();
                for p in positions {
                    if has_claim(c, &session, p, digest, claim.event.as_deref())? {
                        holders.push(p)
                    }
                }
                // py/surface_policy.py:2385-2393 names both causes rather than asserting one.
                match holders.len(){0=>return Err(handle_error("stale-result-handle","result handle no longer names the cited content; rerun the search for a current handle".into())),1=>{center=holders[0];notes.push(json!({"kind":"content_moved","requested":requested,"served":center,"note":format!("handle digest matches turn {center}, not turn {requested} - either the session was renumbered since the handle was minted, or the digest is wrong; verify the content before citing it")}));},_=>return Err(handle_error("content-ambiguous",format!("handle content is ambiguous in session {}: turns {}; rerun the search for a current handle",SessionIndex::default().target(&session),holders.iter().map(i64::to_string).collect::<Vec<_>>().join(", "))))}
            }
        }
    }
    let (lo, hi) = request
        .radius
        .map(|r| (center.saturating_sub(r), center.saturating_add(r)))
        .unwrap_or((first, last));
    let (agent, project, title, family, side) = c.query_row(
        "SELECT agent,project,title,family,side FROM sessions WHERE id=?",
        [&session],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
    )?;
    let rows = load_rows(c, &session, lo, hi)?;
    let events = load_events(c, &session, lo, hi)?;
    Ok(Window {
        target: index.target(&session),
        session,
        agent,
        project,
        title,
        family,
        side,
        center,
        radius: request.radius,
        latest: requested == last && request.turn.is_none() && claim.is_none(),
        first,
        last,
        rows,
        events,
        notes,
        claim,
    })
}
fn has_claim(
    c: &Connection,
    session: &str,
    turn: i64,
    digest: &str,
    event: Option<&str>,
) -> Result<bool> {
    for table in ["prose", "tools"] {
        if event.is_some() && table == "prose" {
            continue;
        }
        let mut q = c.prepare(&format!(
            "SELECT text,ts FROM {table} WHERE session=? AND position=? AND visible=1"
        ))?;
        for row in q.query_map(params![session, turn], |r| {
            Ok((r.get::<_, Vec<u8>>(0)?, r.get::<_, i64>(1)?))
        })? {
            let (raw, ts) = row?;
            let text = store::decode(table, raw)?;
            if content_digest(&text) == digest
                && event.is_none_or(|id| handles::event_id(session, ts, &text) == id)
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}
pub fn load_rows(c: &Connection, session: &str, lo: i64, hi: i64) -> Result<Vec<Row>> {
    let mut q=c.prepare("SELECT p.session,p.position,p.who,p.ts,p.text,p.model,p.model_source,p.source,s.body FROM prose p LEFT JOIN reply_shapes s ON s.source=p.source AND s.session=p.session AND s.turn=p.turn WHERE p.visible=1 AND p.session=? AND p.position BETWEEN ? AND ? ORDER BY p.position,CASE p.who WHEN 'agent' THEN 1 ELSE 0 END,p.ts,p.id")?;
    let mut rows = Vec::new();
    for row in q.query_map(params![session, lo, hi], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, i64>(3)?,
            r.get::<_, Vec<u8>>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
            r.get::<_, i64>(7)?,
            r.get::<_, Option<String>>(8)?,
        ))
    })? {
        let (session, turn, who, ts, bytes, model, model_source, source, shape) = row?;
        let text = store::decode("prose", bytes)?;
        let source_chars = if who == "agent" {
            shape
                .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                .and_then(|v| v["original_reply_chars"].as_u64())
                .map(|v| v as usize)
                .unwrap_or_else(|| text.chars().count())
        } else {
            text.chars().count()
        };
        rows.push(Row {
            session,
            turn,
            who,
            ts,
            text,
            model,
            model_source,
            source,
            source_chars,
        });
    }
    Ok(rows)
}
pub fn load_events(c: &Connection, session: &str, lo: i64, hi: i64) -> Result<Vec<Event>> {
    let mut q=c.prepare("SELECT e.position,e.ts,e.text,e.body FROM events e WHERE e.visible=1 AND e.session=? AND e.position BETWEEN ? AND ? ORDER BY e.position,e.ts,e.call_id")?;
    let mut events = Vec::new();
    let mut seen = BTreeSet::new();
    for r in q.query_map(params![session, lo, hi], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, Option<Vec<u8>>>(2)?,
            r.get::<_, String>(3)?,
        ))
    })? {
        let (turn, ts, bytes, body) = r?;
        let text = bytes
            .map(|bytes| store::decode("tools", bytes))
            .transpose()?
            .unwrap_or_default();
        let raw: Value = serde_json::from_str(&body)?;
        let string = |key: &str| raw[key].as_str().unwrap_or_default().to_owned();
        // Replay copies of one call share its id; separate identical calls stay separate.
        if !seen.insert(string("call_id")) {
            continue;
        }
        let identity = handles::event_id(session, ts, if text.is_empty() { &body } else { &text });
        let kind = raw["kind"].as_str().unwrap_or("tool");
        let (input, output) = if kind == "control" {
            (string("meta"), String::new())
        } else {
            crate::tool_text::split(
                &text,
                kind,
                &string("name"),
                raw["ok"].as_bool(),
                raw["input_end"].as_u64().map(|v| v as usize),
                raw["output_start"].as_u64().map(|v| v as usize),
            )
        };
        events.push(Event {
            turn,
            ts,
            name: string("name"),
            kind: kind.into(),
            input_chars: raw["input_chars"]
                .as_u64()
                .map_or_else(|| input.chars().count(), |v| v as usize),
            output_chars: raw["output_chars"]
                .as_u64()
                .map_or_else(|| output.chars().count(), |v| v as usize),
            output_bytes: raw["output_bytes"].as_u64().map(|v| v as usize),
            ok: raw["ok"].as_bool(),
            input,
            output,
            identity,
            text,
            raw,
        });
    }
    Ok(events)
}
/// Batch all windows on the same connection/snapshot; no source file is reopened.
pub fn windows(c: &Connection, requests: &[Request]) -> Result<Vec<Window>> {
    let index = SessionIndex::load(c)?;
    requests.iter().map(|r| load_in(c, &index, r)).collect()
}

fn resolve_session(
    c: &Connection,
    index: &SessionIndex,
    target: &str,
    claim: Option<&crate::handles::Claim>,
) -> Result<String> {
    let candidates = index.candidates(c, target)?;
    if candidates.is_empty() {
        return Err(handle_error(
            "result-handle-unresolved",
            format!("no indexed session matches {target}; rerun the search for a current handle"),
        ));
    }
    let session = if candidates.len() == 1 {
        candidates[0].clone()
    } else if let Some(claim) = claim {
        let holders: Vec<_> = candidates
            .iter()
            .filter(|s| {
                claim.digest.as_ref().is_some_and(|d| {
                    has_claim(c, s, claim.turn, d, claim.event.as_deref()).unwrap_or(false)
                })
            })
            .cloned()
            .collect();
        if holders.len() != 1 {
            return Err(handle_error(
                "result-handle-unresolved",
                format!(
                    "result handle session is ambiguous: {}",
                    candidates.join(", ")
                ),
            ));
        }
        holders[0].clone()
    } else {
        bail!("session is ambiguous: {}", candidates.join(", "))
    };
    Ok(session)
}
