#[cfg(test)]
use super::connection::schema;
use crate::ingest::registry::{self};
use anyhow::Result;
#[cfg(test)]
use rusqlite::Connection;
use rusqlite::OptionalExtension;
use rusqlite::{params, Transaction};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::Path;

fn family_alias(tx: &Transaction<'_>, agent: &str, alias: &str) -> Result<Option<String>> {
    if tx.query_row("SELECT EXISTS(SELECT 1 FROM sessions s WHERE s.agent=?1 AND s.id=?2 AND EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=s.agent AND p.session=s.id))", params![agent,alias], |r| r.get::<_,bool>(0))? { return Ok(None); }
    let mut q = tx.prepare_cached(
        "SELECT session FROM family_alias_claims WHERE agent=?1 AND alias=?2 LIMIT 2",
    )?;
    let claims: Vec<String> = q
        .query_map(params![agent, alias], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    let [session] = claims.as_slice() else {
        return Ok(None);
    };
    // The original alias pass chose the first unambiguous filename per session.
    let first: Option<String> = tx.query_row("SELECT min(c.alias) FROM family_alias_claims c WHERE c.agent=?1 AND c.session=?2 AND NOT EXISTS(SELECT 1 FROM family_alias_claims d WHERE d.agent=c.agent AND d.alias=c.alias AND d.session!=c.session) AND NOT EXISTS(SELECT 1 FROM sessions s WHERE s.agent=c.agent AND s.id=c.alias AND EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=s.agent AND p.session=s.id))", params![agent,session], |r| r.get(0))?;
    Ok((first.as_deref() == Some(alias)).then(|| session.clone()))
}
pub(super) fn resolve_families(
    tx: &Transaction<'_>,
    changed: &HashSet<(String, String)>,
) -> Result<()> {
    let mut affected = HashSet::new();
    for (agent, id) in changed {
        let parent: Option<String> = tx.query_row("SELECT parent FROM sessions WHERE agent=?1 AND id=?2 AND EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=sessions.agent AND p.session=sessions.id)", params![agent,id], |r| r.get(0)).optional()?;
        let previous: Option<String> = tx
            .query_row(
                "SELECT parent FROM family_nodes WHERE agent=?1 AND id=?2",
                params![agent, id],
                |r| r.get(0),
            )
            .optional()?;
        let old_aliases: BTreeSet<String> = tx
            .prepare_cached("SELECT alias FROM family_alias_claims WHERE agent=?1 AND session=?2")?
            .query_map(params![agent, id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        let mut aliases = BTreeSet::new();
        if parent.is_some() {
            let mut q = tx.prepare_cached("SELECT s.path FROM session_sources ss JOIN sources s ON s.rowid=ss.source WHERE ss.agent=?1 AND ss.session=?2")?;
            for path in q.query_map(params![agent, id], |r| r.get::<_, String>(0))? {
                if let Some(alias) = registry::session_alias(agent, Path::new(&path?), id) {
                    aliases.insert(alias);
                }
            }
        }
        if parent == previous && aliases == old_aliases {
            continue;
        }
        affected.insert((agent.clone(), id.clone()));
        affected.extend(
            aliases
                .iter()
                .chain(&old_aliases)
                .map(|alias| (agent.clone(), alias.clone())),
        );
        // Another claim can change which alias is the first for its target.
        for alias in aliases
            .iter()
            .chain(&old_aliases)
            .chain(std::iter::once(id))
        {
            let targets: Vec<String> = tx
                .prepare_cached(
                    "SELECT session FROM family_alias_claims WHERE agent=?1 AND alias=?2",
                )?
                .query_map(params![agent, alias], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            for target in targets {
                let names: Vec<String> = tx
                    .prepare_cached(
                        "SELECT alias FROM family_alias_claims WHERE agent=?1 AND session=?2",
                    )?
                    .query_map(params![agent, target], |r| r.get(0))?
                    .collect::<rusqlite::Result<_>>()?;
                affected.extend(names.into_iter().map(|name| (agent.clone(), name)));
            }
        }
        tx.execute(
            "DELETE FROM family_alias_claims WHERE agent=?1 AND session=?2",
            params![agent, id],
        )?;
        for alias in aliases {
            tx.prepare_cached("INSERT INTO family_alias_claims VALUES(?1,?2,?3)")?
                .execute(params![agent, alias, id])?;
        }
        if let Some(parent) = parent {
            tx.prepare_cached("INSERT INTO family_nodes VALUES(?1,?2,?3) ON CONFLICT(agent,id) DO UPDATE SET parent=excluded.parent")?.execute(params![agent,id,parent])?;
        } else {
            tx.execute(
                "DELETE FROM family_nodes WHERE agent=?1 AND id=?2",
                params![agent, id],
            )?;
        }
    }
    let mut queue: Vec<_> = affected.iter().cloned().collect();
    while let Some((agent, id)) = queue.pop() {
        let children: Vec<String> = tx
            .prepare_cached("SELECT id FROM sessions WHERE agent=?1 AND parent=?2")?
            .query_map(params![agent, id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        for child in children {
            let key = (agent.clone(), child);
            if affected.insert(key.clone()) {
                queue.push(key);
            }
        }
    }
    let mut memo: HashMap<(String, String), String> = HashMap::new();
    for (agent, id) in affected {
        let mut seen: Vec<String> = vec![];
        let mut cur = id.clone();
        let root = loop {
            if let Some(root) = memo.get(&(agent.clone(), cur.clone())) {
                break root.clone();
            }
            if let Some(at) = seen.iter().position(|s| s == &cur) {
                break seen[at..].iter().min().cloned().unwrap_or(cur);
            }
            seen.push(cur.clone());
            let parent: Option<String> = tx
                .query_row(
                    "SELECT parent FROM sessions WHERE agent=?1 AND id=?2",
                    params![agent, cur],
                    |r| r.get(0),
                )
                .optional()?;
            match parent.filter(|s| !s.is_empty()) {
                Some(p) => cur = family_alias(tx, &agent, &p)?.unwrap_or(p),
                None => break cur,
            }
        };
        for member in seen {
            memo.insert((agent.clone(), member), root.clone());
        }
        tx.prepare_cached("UPDATE sessions SET family=?3 WHERE agent=?1 AND id=?2 AND family!=?3")?
            .execute(params![agent, id, root])?;
    }
    Ok(())
}

#[cfg(test)]
mod family_delta_tests {
    use super::*;
    #[test]
    fn canonical_names_reconsider_other_aliases_of_the_masked_target() {
        let mut c = Connection::open_in_memory().unwrap();
        schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        for (id, parent, family) in [
            ("root", "", "root"),
            ("child", "zzz", "zzz"),
            ("aaa", "", "aaa"),
        ] {
            tx.execute(
                "INSERT INTO sessions VALUES('pi',?1,'p','',?2,?3,0,'title',0,0)",
                params![id, parent, family],
            )
            .unwrap();
            tx.execute("INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra) VALUES(0,'pi',?1,'p',0,'user','m',0,1,x'78','digest','')",[id]).unwrap();
        }
        tx.execute_batch("INSERT INTO family_nodes VALUES('pi','root',''),('pi','child','zzz'); INSERT INTO family_alias_claims VALUES('pi','aaa','root'),('pi','zzz','root');").unwrap();
        let changed = HashSet::from([("pi".into(), "aaa".into())]);
        resolve_families(&tx, &changed).unwrap();
        assert_eq!(
            tx.query_row("SELECT family FROM sessions WHERE id='child'", [], |r| {
                r.get::<_, String>(0)
            })
            .unwrap(),
            "root"
        );
        tx.execute("DELETE FROM prose WHERE session='aaa'", [])
            .unwrap();
        tx.execute("DELETE FROM sessions WHERE id='aaa'", [])
            .unwrap();
        resolve_families(&tx, &changed).unwrap();
        assert_eq!(
            tx.query_row("SELECT family FROM sessions WHERE id='child'", [], |r| {
                r.get::<_, String>(0)
            })
            .unwrap(),
            "zzz"
        );
    }
}
