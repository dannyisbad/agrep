//! Exact family statistics updated from changed prose contributions.
use anyhow::{Context, Result};
use rayon::prelude::*;
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use std::collections::{HashMap, HashSet};

pub(crate) fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS row_stats(id INTEGER PRIMARY KEY,agent TEXT NOT NULL,family TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS family_token_counts(agent TEXT NOT NULL,family TEXT NOT NULL,body BLOB NOT NULL,PRIMARY KEY(agent,family)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stats_dirty(id INTEGER PRIMARY KEY,old_text BLOB,old_agent TEXT,old_family TEXT);
CREATE TRIGGER IF NOT EXISTS prose_stats_insert AFTER INSERT ON prose BEGIN INSERT OR IGNORE INTO stats_dirty(id) VALUES(new.id); END;
CREATE TRIGGER IF NOT EXISTS prose_stats_delete BEFORE DELETE ON prose BEGIN INSERT OR IGNORE INTO stats_dirty SELECT old.id,CASE WHEN r.id IS NOT NULL THEN old.text END,r.agent,r.family FROM (SELECT 1) LEFT JOIN row_stats r ON r.id=old.id; END;
CREATE TRIGGER IF NOT EXISTS prose_stats_update BEFORE UPDATE OF text,visible,agent,session ON prose BEGIN INSERT OR IGNORE INTO stats_dirty SELECT old.id,CASE WHEN r.id IS NOT NULL THEN old.text END,r.agent,r.family FROM (SELECT 1) LEFT JOIN row_stats r ON r.id=old.id; END;
CREATE TRIGGER IF NOT EXISTS session_stats_family BEFORE UPDATE OF family ON sessions WHEN old.family!=new.family BEGIN INSERT OR IGNORE INTO stats_dirty SELECT p.id,CASE WHEN r.id IS NOT NULL THEN p.text END,r.agent,r.family FROM prose p LEFT JOIN row_stats r ON r.id=p.id WHERE p.agent=old.agent AND p.session=old.id; END;")?;
    Ok(())
}
#[derive(Default)]
struct Family {
    key: Option<(String, String)>,
    counts: HashMap<String, [u32; 2]>,
    delta: HashMap<String, [i64; 2]>,
}
impl Family {
    fn flush(&mut self, tx: &Transaction<'_>) -> Result<()> {
        let Some((agent, family)) = &self.key else {
            return Ok(());
        };
        self.counts.retain(|_, v| *v != [0, 0]);
        if self.counts.is_empty() {
            tx.execute(
                "DELETE FROM family_token_counts WHERE agent=?1 AND family=?2",
                params![agent, family],
            )?;
            tx.execute(
                "DELETE FROM family_stats WHERE agent=?1 AND family=?2",
                params![agent, family],
            )?;
        } else {
            let body = lz4_flex::compress_prepend_size(&serde_json::to_vec(&self.counts)?);
            tx.execute("INSERT INTO family_token_counts VALUES(?1,?2,?3) ON CONFLICT(agent,family) DO UPDATE SET body=excluded.body",params![agent,family,body])?;
            let any: HashSet<_> = self
                .counts
                .iter()
                .filter(|(_, n)| n[0] > 0)
                .map(|(t, _)| t)
                .collect();
            let aligned: HashSet<_> = self
                .counts
                .iter()
                .filter(|(_, n)| n[1] > 0)
                .map(|(t, _)| t)
                .collect();
            let body = lz4_flex::compress_prepend_size(&serde_json::to_vec(&(any, aligned))?);
            tx.execute("INSERT INTO family_stats VALUES(?1,?2,?3) ON CONFLICT(agent,family) DO UPDATE SET body=excluded.body",params![agent,family,body])?;
        }
        Ok(())
    }
    fn apply(
        &mut self,
        tx: &Transaction<'_>,
        agent: String,
        family: String,
        text: &str,
        sign: i64,
    ) -> Result<()> {
        let tokens = crate::boundary_stats::text_tokens(std::iter::once(text));
        self.apply_tokens(tx, agent, family, tokens, sign)
    }
    fn apply_tokens(
        &mut self,
        tx: &Transaction<'_>,
        agent: String,
        family: String,
        (any, aligned): (HashSet<String>, HashSet<String>),
        sign: i64,
    ) -> Result<()> {
        if !self
            .key
            .as_ref()
            .is_some_and(|(a, f)| a == &agent && f == &family)
        {
            self.flush(tx)?;
            let body: Option<Vec<u8>> = tx
                .query_row(
                    "SELECT body FROM family_token_counts WHERE agent=?1 AND family=?2",
                    params![agent, family],
                    |r| r.get(0),
                )
                .optional()?;
            self.counts = if let Some(body) = body {
                serde_json::from_slice(&lz4_flex::decompress_size_prepended(&body)?)?
            } else {
                HashMap::new()
            };
            self.key = Some((agent, family));
        }
        for (column, tokens) in [(0, any), (1, aligned)] {
            for token in tokens {
                let n = &mut self.counts.entry(token.clone()).or_default()[column];
                let before = *n > 0;
                *n = if sign > 0 {
                    n.checked_add(1).context("family token count overflow")?
                } else {
                    n.checked_sub(1).context("family token count underflow")?
                };
                if (*n > 0) != before {
                    self.delta.entry(token).or_default()[column] +=
                        i64::from(*n > 0) - i64::from(before);
                }
            }
        }
        Ok(())
    }
}
/// Initial construction has no old contribution and can tokenize rows in parallel.
/// The ordinary refresh below retains its exact changed-row accounting.
pub(crate) fn build(tx: &Transaction<'_>) -> Result<()> {
    let mut family = Family::default();
    let mut q = tx.prepare("SELECT p.agent,s.family,p.text FROM prose p CROSS JOIN sessions s ON s.agent=p.agent AND s.id=p.session WHERE p.visible=1 ORDER BY p.agent,s.family,p.id")?;
    let mut rows = q.query([])?;
    loop {
        let mut batch = Vec::with_capacity(1024);
        for _ in 0..1024 {
            let Some(row) = rows.next()? else { break };
            batch.push((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                String::from_utf8(row.get(2)?)?,
            ));
        }
        if batch.is_empty() {
            break;
        }
        let tokens: Vec<_> = batch
            .into_par_iter()
            .map(|(agent, name, text)| {
                (
                    agent,
                    name,
                    crate::boundary_stats::text_tokens(std::iter::once(text.as_str())),
                )
            })
            .collect();
        for (agent, name, tokens) in tokens {
            family.apply_tokens(tx, agent, name, tokens, 1)?;
        }
    }
    family.flush(tx)?;
    tx.execute("INSERT INTO row_stats SELECT p.id,p.agent,s.family FROM prose p CROSS JOIN sessions s ON s.agent=p.agent AND s.id=p.session WHERE p.visible=1", [])?;
    let mut insert = tx.prepare_cached("INSERT INTO term_stats VALUES(?1,?2,?3)")?;
    for (token, [n, s]) in family.delta {
        insert.execute(params![token, n, s])?;
    }
    tx.execute("DELETE FROM stats_dirty", [])?;
    Ok(())
}
pub(crate) fn refresh(tx: &Transaction<'_>) -> Result<()> {
    // Old contributions were captured by the row/family triggers before mutation.
    if !tx.query_row("SELECT EXISTS(SELECT 1 FROM stats_dirty)", [], |r| {
        r.get::<_, bool>(0)
    })? {
        return Ok(());
    }
    let mut family = Family::default();
    {
        let mut q=tx.prepare("SELECT old_agent,old_family,old_text FROM stats_dirty WHERE old_text IS NOT NULL ORDER BY old_agent,old_family,id")?;
        let mut rows = q.query([])?;
        while let Some(row) = rows.next()? {
            let text = String::from_utf8(row.get(2)?)?;
            family.apply(tx, row.get(0)?, row.get(1)?, &text, -1)?;
        }
    }
    family.flush(tx)?;
    family.key = None;
    tx.execute(
        "DELETE FROM row_stats WHERE id IN (SELECT id FROM stats_dirty)",
        [],
    )?;
    {
        let mut q=tx.prepare("SELECT p.id,p.agent,s.family,p.text FROM stats_dirty d CROSS JOIN prose p ON p.id=d.id CROSS JOIN sessions s ON s.agent=p.agent AND s.id=p.session WHERE p.visible=1 ORDER BY p.agent,s.family,p.id")?;
        let mut rows = q.query([])?;
        let mut insert = tx.prepare_cached("INSERT INTO row_stats VALUES(?1,?2,?3)")?;
        while let Some(row) = rows.next()? {
            let id: i64 = row.get(0)?;
            let agent: String = row.get(1)?;
            let name: String = row.get(2)?;
            let text = String::from_utf8(row.get(3)?)?;
            family.apply(tx, agent.clone(), name.clone(), &text, 1)?;
            insert.execute(params![id, agent, name])?;
        }
    }
    family.flush(tx)?;
    let mut update=tx.prepare_cached("INSERT INTO term_stats VALUES(?1,?2,?3) ON CONFLICT(token) DO UPDATE SET n=n+excluded.n,s=s+excluded.s")?;
    for (token, [n, s]) in family.delta {
        if n != 0 || s != 0 {
            update.execute(params![token, n, s])?;
            if n < 0 {
                tx.execute("DELETE FROM term_stats WHERE token=? AND n<=0", [&token])?;
            }
        }
    }
    tx.execute("DELETE FROM stats_dirty", [])?;
    Ok(())
}
