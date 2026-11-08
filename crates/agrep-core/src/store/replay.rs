use rusqlite::OptionalExtension;

use super::Changes;
use crate::ingest::registry::ADAPTERS;
use anyhow::Result;
use rusqlite::{params, Transaction};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

pub(super) fn source_sessions(
    tx: &Transaction<'_>,
    path: &str,
    out: &mut HashSet<(String, String)>,
) -> Result<()> {
    let mut stmt=tx.prepare_cached("SELECT agent,session FROM prose WHERE source=(SELECT rowid FROM sources WHERE path=?1) UNION SELECT agent,session FROM tools WHERE source=(SELECT rowid FROM sources WHERE path=?1)")?;
    for row in stmt.query_map([path], |r| Ok((r.get(0)?, r.get(1)?)))? {
        out.insert(row?);
    }
    Ok(())
}
struct ReplayRow {
    id: i64,
    source: i64,
    turn: i64,
    position: i64,
    visible: bool,
    who: String,
    ts: i64,
    text: String,
    project: String,
    model: String,
    model_source: String,
}
struct ReplayMessage {
    key_text: String,
    row: usize,
    reply: Option<usize>,
    reply_chars: usize,
    parent: String,
    side: bool,
}
fn replay_cmp(
    a: &ReplayMessage,
    b: &ReplayMessage,
    rows: &[ReplayRow],
    agent: &str,
) -> std::cmp::Ordering {
    let x = &rows[a.row];
    let y = &rows[b.row];
    let reply = |m: &ReplayMessage| m.reply.map_or("", |i| rows[i].text.as_str());
    a.reply_chars
        .cmp(&b.reply_chars)
        .then_with(|| reply(a).len().cmp(&reply(b).len()))
        .then_with(|| (!x.model.is_empty()).cmp(&(!y.model.is_empty())))
        .then_with(|| (!x.project.is_empty()).cmp(&(!y.project.is_empty())))
        .then_with(|| (x.project != agent).cmp(&(y.project != agent)))
        .then_with(|| (!a.parent.is_empty()).cmp(&(!b.parent.is_empty())))
        .then(a.side.cmp(&b.side))
        .then_with(|| match (x.ts > 0, y.ts > 0) {
            (true, false) => std::cmp::Ordering::Greater,
            (false, true) => std::cmp::Ordering::Less,
            (true, true) => y.ts.cmp(&x.ts),
            _ => std::cmp::Ordering::Equal,
        })
        .then_with(|| reply(a).cmp(reply(b)))
        .then(x.model.cmp(&y.model))
        .then(x.project.cmp(&y.project))
        .then(a.parent.cmp(&b.parent))
        .then(x.who.cmp(&y.who))
        .then(x.model_source.cmp(&y.model_source))
}
fn set_visibility(
    tx: &Transaction<'_>,
    table: &str,
    id: i64,
    old: (bool, i64),
    new: (bool, i64),
    changes: &mut Changes,
    bulk: bool,
) -> Result<()> {
    let (old_visible, old_position) = old;
    let (visible, position) = new;
    if old_visible == visible && old_position == position {
        return Ok(());
    }
    if old_visible != visible && !bulk {
        if visible {
            changes.fts.insert(table, id);
        } else {
            changes.fts.delete(table, id);
        }
    }
    // The tools view's INSTEAD OF trigger makes the same event_rows write, after
    // materializing the whole view row, tool text included.
    let sql = if table == "tools" {
        "UPDATE event_rows SET visible=?2,position=?3 WHERE tool_id=?1 AND active=1"
    } else {
        "UPDATE prose SET visible=?2,position=?3 WHERE id=?1"
    };
    tx.prepare_cached(sql)?
        .execute(params![id, visible, position])?;
    changes.touch(table, id);
    Ok(())
}
pub(super) fn session_title(text: &str) -> String {
    crate::history::one_line(text, 120)
}

fn refresh_session_bounds(tx: &Transaction<'_>, agent: &str, session: &str) -> Result<()> {
    // 0.4.9 bounded a chat by its turns alone: a tool row never moves first_ts/last_ts,
    // and a turn without a timestamp (0) is skipped rather than taken as the minimum.
    // Left to itself the planner answers min/max(ts) by walking prose_scope through the
    // agent's whole history, testing each row's session.
    let first: Option<Vec<u8>> = tx.prepare_cached("SELECT text FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2 AND visible=1 AND who NOT IN ('agent','recap') ORDER BY position,id LIMIT 1")?.query_row(params![agent, session], |r| r.get(0)).optional()?;
    let title = first.map_or_else(String::new, |text| {
        session_title(&String::from_utf8_lossy(&text))
    });
    tx.prepare_cached("UPDATE sessions SET first_ts=coalesce((SELECT min(ts) FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2 AND visible=1 AND ts>0),0),last_ts=coalesce((SELECT max(ts) FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2 AND visible=1 AND ts>0),0),title=?3 WHERE agent=?1 AND id=?2")?.execute(params![agent,session,title])?;
    Ok(())
}
/// Chats in `table` (with its session id in `column`) that hold no turn.
fn turnless(tx: &Transaction<'_>, table: &str, column: &str) -> Result<HashSet<(String, String)>> {
    let sql = format!("SELECT agent,{column} FROM {table} t WHERE NOT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.session=t.{column} AND p.agent=t.agent)");
    let found = tx
        .prepare(&sql)?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(found)
}
/// 0.4.9 searched a chat's tool calls only beside its turns, under its first turn's project
/// (py/corpusdb.py:2221-2234): a listed chat without a turn has its calls hidden, and a
/// chat that was turnless when this refresh began shows its calls again once a turn
/// arrives, one copy per call id as in canonicalize.
pub(super) fn settle_turnless(
    tx: &Transaction<'_>,
    before: &HashSet<(String, String)>,
    affected: &HashSet<(String, String)>,
    changes: &mut Changes,
    bulk: bool,
) -> Result<()> {
    let hidden = if bulk {
        turnless(tx, "sessions", "id")?
    } else {
        let mut hidden = HashSet::new();
        let mut q = tx.prepare_cached("SELECT EXISTS(SELECT 1 FROM sessions WHERE agent=?1 AND id=?2) AND NOT EXISTS(SELECT 1 FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2)")?;
        for (agent, session) in affected {
            if q.query_row(params![agent, session], |r| r.get::<_, bool>(0))? {
                hidden.insert((agent.clone(), session.clone()));
            }
        }
        hidden
    };
    let turned = before
        .iter()
        .filter(|s| !hidden.contains(*s))
        .filter_map(|(agent, session)| {
            tx.prepare_cached("SELECT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.session=?2 AND p.agent=?1)")
                .and_then(|mut q| q.query_row(params![agent, session], |r| r.get::<_, bool>(0)))
                .map(|turns| turns.then(|| (agent.clone(), session.clone())))
                .transpose()
        })
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for ((agent, session), turns) in hidden
        .iter()
        .map(|s| (s, false))
        .chain(turned.iter().map(|s| (s, true)))
    {
        let tools:Vec<(i64,i64,bool,String)>=tx.prepare_cached("SELECT t.id,t.position,t.visible,t.extra FROM tools t JOIN sources s ON s.rowid=t.source WHERE t.agent=?1 AND t.session=?2 ORDER BY s.path,t.id")?.query_map(params![agent,session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
        let mut seen = HashSet::new();
        for (id, position, visible, key) in tools {
            let retained = turns && (key.is_empty() || seen.insert(key));
            set_visibility(
                tx,
                "tools",
                id,
                (visible, position),
                (retained, position),
                changes,
                bulk,
            )?;
        }
        if turns {
            tx.prepare_cached("INSERT OR IGNORE INTO symbols(value) SELECT project FROM sessions WHERE agent=?1 AND id=?2")?.execute(params![agent, session])?;
            tx.prepare_cached("UPDATE event_rows SET project_key=(SELECT y.id FROM symbols y JOIN sessions s ON y.value=s.project WHERE s.agent=?1 AND s.id=?2) WHERE active=1 AND session_key=(SELECT id FROM event_sessions WHERE agent=?1 AND session=?2)")?.execute(params![agent, session])?;
        }
    }
    Ok(())
}
/// Keep source contributions for incremental recovery, but index only the canonical replay.
pub(super) fn canonicalize(
    tx: &Transaction<'_>,
    sessions: &HashSet<(String, String)>,
    fast: &HashSet<(String, String)>,
    changes: &mut Changes,
    bulk: bool,
) -> Result<()> {
    let mut pi_order: HashMap<i64, Vec<usize>> = HashMap::new();
    let mut native_order = crate::refresh_order::Order::new(
        ADAPTERS
            .iter()
            .find(|a| a.name() == "pi")
            .unwrap()
            .store_roots(),
    );
    for (agent, session) in sessions {
        if bulk {
            let copies: usize = tx.query_row(
                "SELECT count(*) FROM session_sources WHERE agent=?1 AND session=?2",
                params![agent, session],
                |r| r.get(0),
            )?;
            // Initial metadata was aggregated while inserting this source. Only
            // sessions shared by sources need replay selection and row scans.
            if copies <= 1 {
                continue;
            }
        }
        if fast.contains(&(agent.clone(), session.clone())) {
            let n: i64 = tx.query_row(
                "SELECT count(*) FROM session_sources WHERE agent=?1 AND session=?2",
                params![agent, session],
                |r| r.get(0),
            )?;
            if n <= 1 {
                continue;
            }
        }
        canonicalize_session(
            tx,
            agent,
            session,
            &mut pi_order,
            &mut native_order,
            changes,
            bulk,
        )?;
    }
    Ok(())
}

type ReplaySelection = (
    BTreeMap<(i64, String), usize>,
    Vec<usize>,
    HashMap<usize, i64>,
);
fn replay_order<'a>(
    pi_order: &'a HashMap<i64, Vec<usize>>,
    rows: &[ReplayRow],
    m: &ReplayMessage,
) -> (&'a [usize], usize) {
    (
        pi_order
            .get(&rows[m.row].source)
            .map_or(&[usize::MAX][..], |rank| rank.as_slice()),
        m.row,
    )
}
fn canonicalize_session(
    tx: &Transaction<'_>,
    agent: &str,
    session: &str,
    pi_order: &mut HashMap<i64, Vec<usize>>,
    native_order: &mut crate::refresh_order::Order,
    changes: &mut Changes,
    bulk: bool,
) -> Result<()> {
    let (copies,visible,positioned):(i64,i64,i64)=tx.query_row("SELECT count(DISTINCT source),coalesce(min(visible),1),coalesce(min(position=turn),1) FROM (SELECT source,visible,position,turn FROM prose WHERE agent=?1 AND session=?2 UNION ALL SELECT source,visible,position,turn FROM tools WHERE agent=?1 AND session=?2)",params![agent,session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    refresh_session_bounds(tx, agent, session)?;
    if copies <= 1 && visible != 0 && positioned != 0 {
        return Ok(());
    }
    let rows:Vec<ReplayRow>=tx.prepare("SELECT id,source,turn,position,visible,who,ts,text,project,model,model_source FROM prose WHERE agent=?1 AND session=?2 ORDER BY id")?.query_map(params![agent,session],|r|Ok(ReplayRow{id:r.get(0)?,source:r.get(1)?,turn:r.get(2)?,position:r.get(3)?,visible:r.get(4)?,who:r.get(5)?,ts:r.get(6)?,text:String::from_utf8_lossy(&r.get::<_,Vec<u8>>(7)?).into_owned(),project:r.get(8)?,model:r.get(9)?,model_source:r.get(10)?}))?.collect::<rusqlite::Result<_>>()?;
    let messages = replay_messages(tx, session, &rows)?;
    if agent == "pi" {
        rank_pi_sources(tx, &rows, pi_order, native_order)?;
    }
    let (best, chosen, positions) = choose_replay(&rows, &messages, agent, pi_order);
    let mut keep = HashMap::new();
    let mut mapping = HashMap::new();
    let mut paired = HashSet::new();
    for m in &messages {
        let r = &rows[m.row];
        let winner = best[&(r.turn, m.key_text.clone())];
        mapping.insert((r.source, r.turn), positions[&winner]);
        paired.insert(r.id);
        if let Some(i) = m.reply {
            paired.insert(rows[i].id);
        }
    }
    for ((source, native), position) in &mapping {
        if bulk && native == position {
            // Fresh event rows already carry their native position.
            // Only actual replay collisions need a remapping write.
            continue;
        }
        tx.prepare_cached("UPDATE event_rows SET position=?4 WHERE source=?1 AND session_key IN (SELECT id FROM event_sessions WHERE session=?2) AND native_turn=?3 AND position IS NOT ?4")?.execute(params![source,session,native,position])?;
    }
    for i in &chosen {
        let m = &messages[*i];
        let position = positions[i];
        keep.insert(rows[m.row].id, position);
        if let Some(j) = m.reply {
            keep.insert(rows[j].id, position);
        }
    }
    for r in &rows {
        let position = keep.get(&r.id).copied();
        let retained = position.is_some() || !paired.contains(&r.id);
        set_visibility(
            tx,
            "prose",
            r.id,
            (r.visible, r.position),
            (retained, position.unwrap_or(r.turn)),
            changes,
            bulk,
        )?;
    }
    let tools:Vec<(i64,i64,i64,i64,bool,String)>=tx.prepare("SELECT t.id,t.source,t.turn,t.position,t.visible,t.extra FROM tools t JOIN sources s ON s.rowid=t.source WHERE t.agent=?1 AND t.session=?2 ORDER BY s.path,t.id")?.query_map(params![agent,session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)))?.collect::<rusqlite::Result<_>>()?;
    let mut seen = HashSet::new();
    for (id, source, turn, old_position, old_visible, key) in tools {
        let retained = key.is_empty() || seen.insert(key);
        let position = mapping.get(&(source, turn)).copied().unwrap_or(turn);
        set_visibility(
            tx,
            "tools",
            id,
            (old_visible, old_position),
            (retained, position),
            changes,
            bulk,
        )?;
    }
    refresh_session_bounds(tx, agent, session)?;
    update_replay_metadata(tx, agent, session, &rows, &messages, &chosen, pi_order)?;
    Ok(())
}

fn replay_messages(
    tx: &Transaction<'_>,
    session: &str,
    rows: &[ReplayRow],
) -> Result<Vec<ReplayMessage>> {
    let shapes: HashMap<(i64, i64), serde_json::Value> = tx
    .prepare("SELECT source,turn,body FROM reply_shapes INDEXED BY reply_shapes_session WHERE session=?")?
    .query_map([session], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, String>(2)?,
        ))
    })?
    .map(|r| {
        let (s, t, b) = r?;
        Ok(((s, t), serde_json::from_str(&b)?))
    })
    .collect::<Result<_>>()?;
    let replies: HashMap<_, _> = rows
        .iter()
        .enumerate()
        .filter(|(_, r)| r.who == "agent")
        .map(|(i, r)| ((r.source, r.turn), i))
        .collect();
    let mut messages = Vec::new();
    let prompts: HashSet<_> = rows
        .iter()
        .filter(|r| r.who != "agent")
        .map(|r| (r.source, r.turn))
        .collect();
    for (i, r) in rows
        .iter()
        .enumerate()
        .filter(|(_, r)| r.who != "agent" || !prompts.contains(&(r.source, r.turn)))
    {
        let reply = replies.get(&(r.source, r.turn)).copied();
        let shape = shapes.get(&(r.source, r.turn));
        messages.push(ReplayMessage {
            key_text: if r.who == "agent" {
                String::new()
            } else {
                r.text.clone()
            },
            row: i,
            reply,
            reply_chars: shape
                .and_then(|v| v["original_reply_chars"].as_u64())
                .map_or_else(
                    || reply.map_or(0, |j| rows[j].text.chars().count()),
                    |n| n as usize,
                ),
            parent: shape
                .and_then(|v| v["source_parent"].as_str())
                .unwrap_or("")
                .into(),
            side: shape
                .and_then(|v| v["source_side"].as_bool())
                .unwrap_or(false),
        });
    }
    Ok(messages)
}

fn rank_pi_sources(
    tx: &Transaction<'_>,
    rows: &[ReplayRow],
    pi_order: &mut HashMap<i64, Vec<usize>>,
    native_order: &mut crate::refresh_order::Order,
) -> Result<()> {
    let sources: BTreeSet<_> = rows
        .iter()
        .map(|r| r.source)
        .filter(|id| !pi_order.contains_key(id))
        .collect();
    let mut ranks = Vec::new();
    let mut fallback = false;
    for id in sources {
        let path: String = tx
            .prepare_cached("SELECT path FROM sources WHERE rowid=?")?
            .query_row([id], |r| r.get(0))?;
        if let Some(rank) = native_order.rank(Path::new(&path)) {
            ranks.push((id, rank));
        } else {
            fallback = true;
            break;
        }
    }
    if fallback {
        let paths = crate::ingest::pi::source_order();
        *pi_order = tx
            .prepare("SELECT rowid,path FROM sources WHERE agent='pi'")?
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?
            .map(|row| {
                let (id, path) = row?;
                Ok((
                    id,
                    vec![paths.get(Path::new(&path)).copied().unwrap_or(usize::MAX)],
                ))
            })
            .collect::<rusqlite::Result<_>>()?;
    } else {
        pi_order.extend(ranks);
    }
    Ok(())
}

fn choose_replay(
    rows: &[ReplayRow],
    messages: &[ReplayMessage],
    agent: &str,
    pi_order: &HashMap<i64, Vec<usize>>,
) -> ReplaySelection {
    let source_order = |m: &ReplayMessage| replay_order(pi_order, rows, m);
    let mut best: BTreeMap<(i64, String), usize> = BTreeMap::new();
    for (i, m) in messages.iter().enumerate() {
        let r = &rows[m.row];
        let key = (r.turn, m.key_text.clone());
        if best.get(&key).is_none_or(|j| {
            let prior = &messages[*j];
            let quality = replay_cmp(m, prior, rows, agent);
            quality.is_gt() || (quality.is_eq() && source_order(m) < source_order(prior))
        }) {
            best.insert(key, i);
        }
    }
    let mut chosen: Vec<usize> = best.values().copied().collect();
    let collision = chosen
        .iter()
        .map(|i| rows[messages[*i].row].turn)
        .collect::<HashSet<_>>()
        .len()
        < chosen.len();
    if collision {
        chosen.sort_by_key(|i| {
            let r = &rows[messages[*i].row];
            (r.ts <= 0, r.ts, r.turn, r.text.as_str())
        });
    }
    let positions: HashMap<usize, i64> = chosen
        .iter()
        .enumerate()
        .map(|(n, i)| {
            (
                *i,
                if collision {
                    n as i64
                } else {
                    rows[messages[*i].row].turn
                },
            )
        })
        .collect();
    (best, chosen, positions)
}

fn update_replay_metadata(
    tx: &Transaction<'_>,
    agent: &str,
    session: &str,
    rows: &[ReplayRow],
    messages: &[ReplayMessage],
    chosen: &[usize],
    pi_order: &HashMap<i64, Vec<usize>>,
) -> Result<()> {
    let source_order = |m: &ReplayMessage| replay_order(pi_order, rows, m);
    if let Some(m) = chosen
        .iter()
        .map(|i| &messages[*i])
        .min_by_key(|m| source_order(m))
    {
        let r = &rows[m.row];
        let claims: Vec<_> = chosen
            .iter()
            .map(|i| &messages[*i])
            .filter(|m| !m.parent.is_empty())
            .collect();
        let parent = claims
            .into_iter()
            .min_by_key(|m| source_order(m))
            .map_or("", |m| m.parent.as_str());
        let title = chosen
            .iter()
            .map(|i| &messages[*i])
            .filter(|m| !matches!(rows[m.row].who.as_str(), "agent" | "recap"))
            .filter(|m| !rows[m.row].text.trim().is_empty())
            .min_by_key(|m| source_order(m))
            .map(|m| session_title(&rows[m.row].text))
            .unwrap_or_default();
        tx.execute(
            "UPDATE sessions SET project=?3,parent=?4,side=?5,title=?6 WHERE agent=?1 AND id=?2",
            params![
                agent,
                session,
                r.project,
                parent,
                m.side || !parent.is_empty(),
                title
            ],
        )?;
    }
    Ok(())
}
