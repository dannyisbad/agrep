//! FTS5 segment upkeep. A refresh adds at most one segment to each FTS table and never
//! merges; a detached worker merges them in bounded steps, so lookups, which seek
//! every segment for every term, stay on few segments, and no foreground writer
//! waits for more than one step.
use crate::{data::DataRoot, store};
use anyhow::{Context, Result};
use rusqlite::Connection;
use serde::Serialize;
use std::time::Instant;

/// Every FTS5 table that queries read.
const TABLES: [&str; 3] = ["prose_fts", "tools_fts", "tool_lexicon"];

/// One level of an FTS5 index: the number of its segments that are inputs to an
/// unfinished merge, and each segment's size in leaf pages, oldest first.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Level {
    pub merging: u64,
    pub pages: Vec<u64>,
}

/// The levels of a table's index, from its FTS5 structure record.
pub fn levels(c: &Connection, table: &str) -> Result<Vec<Level>> {
    let record: Vec<u8> = c.query_row(
        &format!("SELECT block FROM {table}_data WHERE id=10"),
        [],
        |r| r.get(0),
    )?;
    // A 4-byte cookie, then an optional version 2 marker; version 2 segments carry
    // origin, tombstone and entry counts after their page range.
    let v2 = record.get(4..8) == Some(&[0xff, 0, 0, 1][..]);
    let mut at = if v2 { 8 } else { 4 };
    let count = varint(&record, &mut at)?;
    varint(&record, &mut at)?;
    varint(&record, &mut at)?;
    let mut out = Vec::new();
    for _ in 0..count {
        let merging = varint(&record, &mut at)?;
        let segments = varint(&record, &mut at)?;
        let mut pages = Vec::new();
        for _ in 0..segments {
            varint(&record, &mut at)?;
            let first = varint(&record, &mut at)?;
            let last = varint(&record, &mut at)?;
            for _ in 0..if v2 { 5 } else { 0 } {
                varint(&record, &mut at)?;
            }
            pages.push((last + 1).saturating_sub(first));
        }
        out.push(Level { merging, pages });
    }
    Ok(out)
}

fn varint(b: &[u8], at: &mut usize) -> Result<u64> {
    let mut v = 0u64;
    for i in 0..9 {
        let x = *b.get(*at).context("truncated FTS5 structure record")?;
        *at += 1;
        if i == 8 {
            return Ok((v << 8) | u64::from(x));
        }
        v = (v << 7) | u64::from(x & 0x7f);
        if x < 0x80 {
            break;
        }
    }
    Ok(v)
}

/// Settings for writes after the first build: refresh flushes never merge
/// (`automerge=0`), `merge` steps merge a level once it holds two segments, and a
/// refresh's pending terms stay in memory up to `hashsize` bytes.
pub(crate) fn configure(c: &Connection, table: &str) -> Result<()> {
    for (key, value) in [
        ("automerge", 0),
        ("usermerge", 2),
        ("crisismerge", 16),
        ("hashsize", 8 << 20),
    ] {
        c.execute(
            &format!("INSERT INTO {table}({table},rank) VALUES(?1,?2)"),
            rusqlite::params![key, value],
        )?;
    }
    Ok(())
}

pub fn segments(levels: &[Level]) -> usize {
    levels.iter().map(|l| l.pages.len()).sum()
}

/// Whether [`upkeep`] has work: an unfinished merge, a level holding two segments, or
/// more than [`CAP`] segments in a table.
pub fn pending(c: &Connection) -> Result<bool> {
    for table in TABLES {
        let levels = levels(c, table)?;
        if segments(&levels) > CAP || levels.iter().any(|l| l.merging > 0 || l.pages.len() > 1) {
            return Ok(true);
        }
    }
    Ok(false)
}

#[derive(Debug, Default, Serialize)]
pub struct Upkeep {
    pub tables: Vec<TableUpkeep>,
    /// Another process held `merge.lock`, so this one merged nothing.
    pub skipped_lock: bool,
    pub checkpoint_ms: f64,
    /// The longest step this process committed.
    pub max_step_ms: f64,
    /// Steps rolled back because an explicit command started meanwhile.
    pub preempted: usize,
    pub ms: f64,
}
#[derive(Debug, Default, Serialize)]
pub struct TableUpkeep {
    pub table: &'static str,
    pub segments_before: usize,
    pub segments_after: usize,
    pub merge_steps: usize,
    pub optimize_steps: usize,
    pub ms: f64,
}

/// Merge each table's segments; see [`upkeep_with`].
pub fn upkeep(root: &DataRoot) -> Result<Upkeep> {
    upkeep_with(root, CAP, STEP_PAGES)
}

/// More segments than this after the level merges cost a full merge of the table.
/// Each segment adds a few percent to lookups; each step up halves how often the
/// large segment is rewritten (see docs/ENGINE.md).
pub const CAP: usize = 5;
/// Output leaf pages a step asks for. FTS5 ends a step only between terms, so a
/// common trigram's doclist still makes one step of thousands of pages; a small budget
/// keeps every other step to a few milliseconds.
const STEP_PAGES: i64 = 64;
/// WAL bytes the merge leaves before it checkpoints them itself: well below the
/// 4096-page auto-checkpoint that would make a foreground commit copy them.
const CHECKPOINT_BYTES: u64 = 4 << 20;

/// Level merges come first: with `usermerge=2` FTS5 merges any level that holds two
/// segments into the next, so each refresh's segment carries upward like a binary
/// counter and the large segment is rewritten only when a carry reaches its level.
/// When more than `cap` segments remain, the whole table is merged into one. Every
/// step is its own transaction under a writer lock taken only while no writer is
/// queued, and the worker checkpoints the WAL it grows. One process merges at a
/// time, under `merge.lock`.
fn upkeep_with(root: &DataRoot, cap: usize, step_pages: i64) -> Result<Upkeep> {
    let start = Instant::now();
    let mut out = Upkeep::default();
    if !root.db().exists() {
        return Ok(out);
    }
    loop {
        let Some(merging) = root.merge_lock()? else {
            out.skipped_lock = out.tables.is_empty();
            break;
        };
        let c = {
            root.wait_for_foreground()?;
            let _lock = root.lock_behind_writers()?;
            store::writer(root)?
        };
        c.execute_batch("PRAGMA wal_autocheckpoint=0")?;
        let mut worked = false;
        for table in TABLES {
            let table_start = Instant::now();
            let before = segments(&levels(&c, table)?);
            let mut report = TableUpkeep {
                table,
                segments_before: before,
                ..Default::default()
            };
            if before > 1 {
                report.merge_steps = steps(root, &c, table, step_pages, &mut out)?;
            }
            if segments(&levels(&c, table)?) > cap {
                report.optimize_steps = steps(root, &c, table, -step_pages, &mut out)?;
            }
            report.segments_after = segments(&levels(&c, table)?);
            report.ms = table_start.elapsed().as_secs_f64() * 1000.;
            worked |= report.merge_steps + report.optimize_steps > 0;
            out.tables.push(report);
        }
        drop(merging);
        // A writer that found merge.lock held while this process finished its last step
        // left its new segment to this process.
        if !worked || !pending(&c)? {
            break;
        }
    }
    out.ms = start.elapsed().as_secs_f64() * 1000.;
    Ok(out)
}

/// SQLite virtual-machine instructions between checks for an explicit command during a
/// step: FTS5 writes each output page through a statement, so about a millisecond.
const PROBE_OPS: i32 = 1000;

/// Run FTS5 `merge` steps (a negative budget merges every segment into one) until a
/// step leaves the structure record unchanged. A step starts only while no explicit
/// command runs, and one that starts meanwhile interrupts it: the step rolls back, its
/// writer lock is free at once, and the step runs again once the command has exited.
fn steps(
    root: &DataRoot,
    c: &Connection,
    table: &str,
    pages: i64,
    out: &mut Upkeep,
) -> Result<usize> {
    let mut wal = root.db().into_os_string();
    wal.push("-wal");
    let probe = std::sync::Arc::new(root.foreground_probe()?);
    let mut n = 0;
    loop {
        root.wait_for_foreground()?;
        let lock = root.lock_behind_writers()?;
        let started = Instant::now();
        c.execute_batch("BEGIN IMMEDIATE")?;
        let changed = step(c, table, pages, &probe, PROBE_OPS);
        let changed = match changed {
            Ok(Some(changed)) => {
                c.execute_batch("COMMIT")?;
                changed
            }
            Ok(None) => {
                // An interrupted write statement has already rolled its transaction back.
                if !c.is_autocommit() {
                    c.execute_batch("ROLLBACK")?;
                }
                out.preempted += 1;
                continue;
            }
            Err(e) => {
                if !c.is_autocommit() {
                    c.execute_batch("ROLLBACK")?;
                }
                return Err(e);
            }
        };
        drop(lock);
        out.max_step_ms = out.max_step_ms.max(started.elapsed().as_secs_f64() * 1000.);
        // PASSIVE never waits: a writer that started meanwhile appends after these frames.
        if !changed || std::fs::metadata(&wal).is_ok_and(|m| m.len() >= CHECKPOINT_BYTES) {
            let started = Instant::now();
            c.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |_| Ok(()))?;
            out.checkpoint_ms += started.elapsed().as_secs_f64() * 1000.;
        }
        if !changed {
            return Ok(n);
        }
        n += 1;
    }
}

/// One merge step inside the caller's transaction: whether it changed the structure,
/// or `None` when an explicit command started during it and interrupted it.
fn step(
    c: &Connection,
    table: &str,
    pages: i64,
    probe: &std::sync::Arc<crate::data::ForegroundProbe>,
    probe_ops: i32,
) -> Result<Option<bool>> {
    let record = || -> Result<Vec<u8>> {
        Ok(c.query_row(
            &format!("SELECT block FROM {table}_data WHERE id=10"),
            [],
            |r| r.get(0),
        )?)
    };
    let before = record()?;
    let watch = std::sync::Arc::clone(probe);
    c.progress_handler(probe_ops, Some(move || watch.active()));
    let merged = c.execute(
        &format!("INSERT INTO {table}({table},rank) VALUES('merge',?1)"),
        [pages],
    );
    c.progress_handler(0, None::<fn() -> bool>);
    match merged {
        Err(rusqlite::Error::SqliteFailure(e, _))
            if e.code == rusqlite::ErrorCode::OperationInterrupted =>
        {
            Ok(None)
        }
        Err(e) => Err(e.into()),
        Ok(_) => Ok(Some(record()? != before)),
    }
}

/// The level a first build's lone segment moves to. The binary counter of later
/// refreshes occupies levels 0-4 before the cap forces a whole-table merge (five
/// segments below it after 31 refreshes), so it never carries into this level.
const SEAL_LEVEL: u8 = 16;

/// A first build leaves its one segment per table at level 1, where the second
/// refresh's carry would rewrite it whole to absorb a few pages, and again at the
/// next carries. Moved above the levels later refreshes reach, it is rewritten only
/// by the merge the cap forces, which puts the result on a new top level. FTS5 still
/// pulls a segment no larger than a newly written one down beside it, so only a first
/// build that outweighs later segments stays sealed, which is when a rewrite costs.
/// Run on a connection that has not used the table, so no cached FTS5 structure goes
/// stale.
pub(crate) fn seal(c: &Connection) -> Result<()> {
    for table in TABLES {
        let record: Vec<u8> = c.query_row(
            &format!("SELECT block FROM {table}_data WHERE id=10"),
            [],
            |r| r.get(0),
        )?;
        let v2 = record.get(4..8) == Some(&[0xff, 0, 0, 1][..]);
        let head = if v2 { 8 } else { 4 };
        let mut at = head;
        let count = varint(&record, &mut at)?;
        let counters = at;
        if varint(&record, &mut at)? != 1 {
            continue;
        }
        varint(&record, &mut at)?;
        let counters = record[counters..at].to_vec();
        for _ in 0..count {
            let merging = varint(&record, &mut at)?;
            if varint(&record, &mut at)? == 0 {
                continue;
            }
            anyhow::ensure!(merging == 0, "{table}: the lone segment is being merged");
            let segment = at;
            for _ in 0..if v2 { 8 } else { 3 } {
                varint(&record, &mut at)?;
            }
            let mut sealed = record[..head].to_vec();
            sealed.push(SEAL_LEVEL + 1);
            sealed.extend(&counters);
            sealed.extend([0, 0].repeat(SEAL_LEVEL.into()));
            sealed.extend([0, 1]);
            sealed.extend(&record[segment..at]);
            c.execute(
                &format!("UPDATE {table}_data SET block=?1 WHERE id=10"),
                [sealed],
            )?;
            break;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn table(c: &Connection) {
        c.execute_batch("CREATE VIRTUAL TABLE t USING fts5(text,content='',contentless_delete=1,tokenize='unicode61',detail=full);INSERT INTO t(t,rank) VALUES('automerge',0);").unwrap();
    }

    #[test]
    fn structure_record_lists_each_flushed_segment() {
        let c = Connection::open_in_memory().unwrap();
        table(&c);
        for id in 1..=3 {
            c.execute("INSERT INTO t(rowid,text) VALUES(?1,'alpha beta')", [id])
                .unwrap();
        }
        assert_eq!(segments(&levels(&c, "t").unwrap()), 3);
        c.execute("INSERT INTO t(t) VALUES('optimize')", [])
            .unwrap();
        let levels = levels(&c, "t").unwrap();
        assert_eq!(segments(&levels), 1);
        assert!(levels.iter().all(|l| l.merging == 0));
    }

    #[test]
    fn segments_beyond_the_cap_merge_into_one_and_keep_every_row() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path(), true).unwrap();
        let c = store::writer(&root).unwrap();
        // No level merge applies below 16 segments, so only the cap can act.
        c.execute(
            "INSERT INTO prose_fts(prose_fts,rank) VALUES('usermerge',16)",
            [],
        )
        .unwrap();
        let rows = CAP as i64 + 2;
        for id in 1..=rows {
            c.execute(
                "INSERT INTO prose_fts(rowid,text) VALUES(?1,'alpha beta')",
                [id],
            )
            .unwrap();
        }
        assert_eq!(segments(&levels(&c, "prose_fts").unwrap()), CAP + 2);
        let report = upkeep(&root).unwrap();
        let prose = &report.tables[0];
        assert_eq!((prose.segments_before, prose.segments_after), (CAP + 2, 1));
        assert!(prose.optimize_steps > 0);
        assert_eq!(segments(&levels(&c, "prose_fts").unwrap()), 1);
        let matched: i64 = c
            .query_row(
                "SELECT count(*) FROM prose_fts WHERE prose_fts MATCH 'alpha'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(matched, rows);
        let again = upkeep(&root).unwrap();
        assert!(again
            .tables
            .iter()
            .all(|t| t.merge_steps + t.optimize_steps == 0));
    }

    #[test]
    fn a_step_rolls_back_as_soon_as_an_explicit_command_starts() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path(), true).unwrap();
        let c = store::writer(&root).unwrap();
        c.execute_batch("INSERT INTO prose_fts(prose_fts,rank) VALUES('automerge',0);INSERT INTO prose_fts(prose_fts,rank) VALUES('usermerge',2);").unwrap();
        for id in 1..=2 {
            c.execute(
                "INSERT INTO prose_fts(rowid,text) VALUES(?1,'alpha beta')",
                [id],
            )
            .unwrap();
        }
        let before = levels(&c, "prose_fts").unwrap();
        assert_eq!(segments(&before), 2);
        let probe = std::sync::Arc::new(root.foreground_probe().unwrap());
        let command = root.foreground().unwrap();
        c.execute_batch("BEGIN IMMEDIATE").unwrap();
        assert_eq!(step(&c, "prose_fts", 64, &probe, 1).unwrap(), None);
        if !c.is_autocommit() {
            c.execute_batch("ROLLBACK").unwrap();
        }
        assert_eq!(levels(&c, "prose_fts").unwrap(), before);
        drop(command);
        c.execute_batch("BEGIN IMMEDIATE").unwrap();
        assert_eq!(step(&c, "prose_fts", 64, &probe, 1).unwrap(), Some(true));
        c.execute_batch("COMMIT").unwrap();
        assert_eq!(segments(&levels(&c, "prose_fts").unwrap()), 1);
    }
}
