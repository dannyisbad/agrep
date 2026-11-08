#[cfg(test)]
use super::connection::schema;
use super::connection::writer;
#[cfg(test)]
use super::rows::{erase, upsert, NewRow};
use super::text::{decode, search_fold};
use super::{Changes, BLOCK};
use crate::data::DataRoot;
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::OptionalExtension;
use rusqlite::{params, Connection, Transaction};
use std::collections::{BTreeSet, HashMap, HashSet};

pub fn grams(text: &str) -> HashMap<String, u32> {
    let cs: Vec<_> = text.chars().collect();
    let mut g = HashMap::new();
    for width in 1..=3 {
        for w in cs.windows(width) {
            *g.entry(w.iter().collect()).or_default() += 1;
        }
    }
    g
}
type BoundRow = (Vec<u8>, i64, String, String, i64);
struct BuiltBounds {
    key: i64,
    max_ts: i64,
    user_ts: Option<i64>,
    weight: f64,
    min_session: String,
    min_turn: i64,
    grams: Vec<(u32, GramStat)>,
    max_gap: usize,
    all_ascii: bool,
    append: bool,
}
#[derive(Clone, Copy, Debug, Default)]
struct GramStat {
    frequency: u32,
    max_ts: i64,
    weight: u8,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Posting {
    pub block: u32,
    pub frequency: u32,
    pub max_ts: i64,
    pub weight: u8,
}
fn weight_code(who: &str) -> u8 {
    match who {
        "tool" => 0,
        "harness" | "synthetic" => 1,
        "recap" | "control" => 2,
        "subagent" => 4,
        "agent" => 5,
        "user" => 6,
        _ => 3,
    }
}
pub(crate) fn posting_weight(code: u8) -> f64 {
    [0.22, 0.3, 0.325, 0.5, 0.7225, 0.8, 1.][usize::from(code)]
}
fn gram_maxima(texts: impl Iterator<Item = (String, i64, String)>) -> HashMap<u32, GramStat> {
    // The dense key also records only metadata of rows containing this gram.
    // A recent non-match must not raise the recency ceiling of old matches.
    let mut counts = vec![(usize::MAX, 0u32, GramStat::default()); 47988];
    let mut touched = Vec::new();
    for (row, (text, ts, who)) in texts.enumerate() {
        let code = weight_code(&who);
        let mut observe = |key: usize| {
            let (previous, current, stat) = &mut counts[key];
            if stat.frequency == 0 {
                touched.push(key as u32);
            }
            if *previous != row {
                *previous = row;
                *current = 0;
                stat.max_ts = stat.max_ts.max(ts);
                stat.weight = stat.weight.max(code);
            }
            *current += 1;
            stat.frequency = stat.frequency.max(*current);
        };
        let (mut rolling, mut run) = (0usize, 0usize);
        for byte in text.bytes() {
            let digit = match byte {
                b'0'..=b'9' => usize::from(byte - b'0'),
                b'a'..=b'z' => 10 + usize::from(byte - b'a'),
                _ => {
                    rolling = 0;
                    run = 0;
                    continue;
                }
            };
            rolling = (rolling % 1296) * 36 + digit;
            run = (run + 1).min(3);
            observe(digit);
            if run >= 2 {
                observe(36 + rolling % 1296);
            }
            if run == 3 {
                observe(1332 + rolling);
            }
        }
    }
    touched
        .into_iter()
        .map(|key| (key, counts[key as usize].2))
        .collect()
}

fn build_bounds(key: i64, rows: Vec<BoundRow>) -> Result<BuiltBounds> {
    let table = if key < 0 { "tools" } else { "prose" };
    let mut max_ts = 0;
    let mut user_ts = None;
    let mut weight: f64 = 0.;
    let mut min_session = String::new();
    let mut min_turn = i64::MAX;
    let mut texts = Vec::with_capacity(rows.len());
    let mut max_gap = 0;
    let mut all_ascii = true;
    for (text, ts, who, session, turn) in rows {
        if min_session.is_empty() || session < min_session {
            min_session = session.clone();
            min_turn = turn;
        }
        if session == min_session {
            min_turn = min_turn.min(turn);
        }
        max_ts = max_ts.max(ts);
        weight = weight.max(crate::query::weight(&who));
        if who == "user" {
            user_ts = Some(user_ts.unwrap_or(0).max(ts));
        }
        let decoded = decode(table, text)?;
        all_ascii &= decoded.is_ascii();
        let text = search_fold(&decoded);
        max_gap = max_gap.max(crate::fts_span::gap_upper(&text));
        texts.push((text, ts, who));
    }
    let mut grams: Vec<_> = gram_maxima(texts.into_iter()).into_iter().collect();
    grams.sort_unstable_by_key(|a| a.0);
    Ok(BuiltBounds {
        key,
        max_ts,
        user_ts,
        weight,
        min_session,
        min_turn,
        grams,
        max_gap,
        all_ascii,
        append: false,
    })
}
/// Dense ASCII keys cover common keyword bounds; other text retains a
/// conservative universal ceiling and its complete FTS/verification path.
fn gram_code(gram: &str) -> Option<u32> {
    if !(1..=3).contains(&gram.len()) {
        return None;
    }
    let mut value = 0;
    for b in gram.bytes() {
        let digit = match b {
            b'0'..=b'9' => u32::from(b - b'0'),
            b'a'..=b'z' => 10 + u32::from(b - b'a'),
            _ => return None,
        };
        value = value * 36 + digit;
    }
    Some(
        value
            + match gram.len() {
                1 => 0,
                2 => 36,
                _ => 1332,
            },
    )
}
pub(crate) fn indexed_gram(gram: &str) -> bool {
    gram_code(gram).is_some()
}
#[cfg(test)]
fn gram_text(code: u32) -> Result<String> {
    let (mut n, len) = if code < 36 {
        (code, 1)
    } else if code < 1332 {
        (code - 36, 2)
    } else if code < 47988 {
        (code - 1332, 3)
    } else {
        anyhow::bail!("invalid gram key")
    };
    let mut out = vec![0; len];
    for byte in out.iter_mut().rev() {
        let d = (n % 36) as u8;
        *byte = if d < 10 { b'0' + d } else { b'a' + d - 10 };
        n /= 36;
    }
    Ok(String::from_utf8(out)?)
}
pub(crate) fn put_varint(mut n: u32, out: &mut Vec<u8>) {
    while n >= 128 {
        out.push(n as u8 | 128);
        n >>= 7;
    }
    out.push(n as u8);
}
pub(crate) fn take_varint(bytes: &[u8], at: &mut usize) -> Result<u32> {
    let mut n = 0;
    for shift in (0..=28).step_by(7) {
        let b = *bytes
            .get(*at)
            .ok_or_else(|| anyhow::anyhow!("truncated integer list"))?;
        *at += 1;
        anyhow::ensure!(shift != 28 || b < 16, "integer list overflow");
        n |= u32::from(b & 127) << shift;
        if b < 128 {
            return Ok(n);
        }
    }
    anyhow::bail!("invalid integer list")
}
const GRAM_BITMAP_BYTES: usize = 47988usize.div_ceil(8);
fn pack_grams(grams: &[(u32, GramStat)]) -> Vec<u8> {
    let mut bits = vec![0; GRAM_BITMAP_BYTES];
    for &(key, _) in grams {
        bits[key as usize / 8] |= 1 << (key % 8);
    }
    lz4_flex::compress_prepend_size(&bits)
}
fn gram_bitmap(bytes: &[u8]) -> Result<Vec<u8>> {
    let bits = lz4_flex::decompress_size_prepended(bytes)?;
    anyhow::ensure!(
        bits.len() == GRAM_BITMAP_BYTES && bits[GRAM_BITMAP_BYTES - 1] & 0xf0 == 0,
        "invalid gram bitmap"
    );
    Ok(bits)
}
fn bitmap_grams(bits: &[u8]) -> impl Iterator<Item = u32> + '_ {
    bits.iter().enumerate().flat_map(|(index, &byte)| {
        (0..8)
            .filter(move |bit| byte & (1 << bit) != 0)
            .map(move |bit| (index * 8 + bit) as u32)
    })
}
#[cfg(test)]
fn unpack_grams(bytes: &[u8]) -> Result<Vec<u32>> {
    Ok(bitmap_grams(&gram_bitmap(bytes)?).collect())
}
fn put_wide(mut n: u64, out: &mut Vec<u8>) {
    while n >= 128 {
        out.push(n as u8 | 128);
        n >>= 7;
    }
    out.push(n as u8);
}
fn take_wide(bytes: &[u8], at: &mut usize) -> Result<u64> {
    let mut n = 0;
    for shift in (0..=63).step_by(7) {
        let b = *bytes
            .get(*at)
            .ok_or_else(|| anyhow::anyhow!("truncated timestamp"))?;
        *at += 1;
        anyhow::ensure!(shift != 63 || b < 2, "timestamp overflow");
        n |= u64::from(b & 127) << shift;
        if b < 128 {
            return Ok(n);
        }
    }
    anyhow::bail!("invalid timestamp")
}
const POSTING_CHUNK: u32 = 64;
pub(crate) fn posting_list(c: &Connection, lane: i64, gram: &str) -> Result<Vec<Posting>> {
    let Some(key) = gram_code(gram) else {
        return Ok(vec![]);
    };
    // One statement keeps both halves on the same snapshot while a worker folds raises.
    let mut q = c.prepare_cached("SELECT 0,chunk,body,NULL,NULL,NULL FROM gram_postings WHERE lane=?1 AND gram=?2 UNION ALL SELECT 1,block,NULL,frequency,max_ts,weight FROM gram_raises WHERE lane=?1 AND gram=?2")?;
    let mut rows = q.query(params![lane, key])?;
    let mut base = Vec::new();
    let mut raises = Vec::new();
    while let Some(row) = rows.next()? {
        if row.get::<_, i64>(0)? == 0 {
            base.extend(decode_posting(row.get_ref(2)?.as_blob()?)?);
        } else {
            let posting = Posting {
                block: row.get(1)?,
                frequency: row.get(3)?,
                max_ts: row.get(4)?,
                weight: row.get(5)?,
            };
            anyhow::ensure!(
                posting.frequency > 0 && posting.max_ts >= 0 && posting.weight <= 6,
                "invalid gram raise"
            );
            raises.push(posting);
        }
    }
    if base.windows(2).any(|p| p[0].block > p[1].block) {
        base.sort_unstable_by_key(|p| p.block);
    }
    if raises.is_empty() {
        return Ok(base);
    }
    raises.sort_unstable_by_key(|p| p.block);
    let mut out = Vec::with_capacity(base.len() + raises.len());
    let mut base = base.into_iter().peekable();
    for mut raised in raises {
        while base.peek().is_some_and(|p| p.block < raised.block) {
            out.push(base.next().expect("posting before raise"));
        }
        if base.peek().is_some_and(|p| p.block == raised.block) {
            let prior = base.next().expect("same posting");
            raised.frequency = raised.frequency.max(prior.frequency);
            raised.max_ts = raised.max_ts.max(prior.max_ts);
            raised.weight = raised.weight.max(prior.weight);
        }
        out.push(raised);
    }
    out.extend(base);
    Ok(out)
}

fn posting_chunk(c: &Connection, lane: i64, key: u32, chunk: u32) -> Result<Vec<Posting>> {
    let bytes: Option<Vec<u8>> = c
        .prepare_cached("SELECT body FROM gram_postings WHERE lane=?1 AND gram=?2 AND chunk=?3")?
        .query_row(params![lane, key, chunk], |r| r.get(0))
        .optional()?;
    bytes.map_or_else(|| Ok(vec![]), |bytes| decode_posting(&bytes))
}
fn decode_posting(bytes: &[u8]) -> Result<Vec<Posting>> {
    let packed = lz4_flex::decompress_size_prepended(bytes)?;
    let data = packed
        .strip_prefix(b"R1")
        .ok_or_else(|| anyhow::anyhow!("invalid posting format"))?;
    let (mut at, mut last, mut timestamp, mut rows) = (0, 0u32, 0i64, Vec::new());
    while at < data.len() {
        last = last
            .checked_add(take_varint(data, &mut at)?)
            .ok_or_else(|| anyhow::anyhow!("block id overflow"))?;
        let frequency = take_varint(data, &mut at)?;
        anyhow::ensure!(frequency > 0, "zero gram frequency");
        let zigzag = take_wide(data, &mut at)?;
        let delta = (zigzag >> 1) as i64 ^ -((zigzag & 1) as i64);
        timestamp = timestamp
            .checked_add(delta)
            .ok_or_else(|| anyhow::anyhow!("timestamp overflow"))?;
        anyhow::ensure!(timestamp >= 0, "negative posting timestamp");
        let weight = *data
            .get(at)
            .ok_or_else(|| anyhow::anyhow!("missing posting weight"))?;
        at += 1;
        anyhow::ensure!(weight <= 6, "invalid posting weight");
        rows.push(Posting {
            block: last,
            frequency,
            max_ts: timestamp,
            weight,
        });
    }
    Ok(rows)
}
#[cfg(test)]
fn write_posting(tx: &Transaction<'_>, lane: i64, gram: &str, rows: &[Posting]) -> Result<()> {
    let mut at = 0;
    while at < rows.len() {
        let chunk = rows[at].block / POSTING_CHUNK;
        let end = at + rows[at..].partition_point(|r| r.block / POSTING_CHUNK == chunk);
        write_posting_chunk(
            tx,
            lane,
            gram_code(gram).expect("test gram"),
            chunk,
            &rows[at..end],
        )?;
        at = end;
    }
    Ok(())
}
fn write_posting_chunk(
    tx: &Transaction<'_>,
    lane: i64,
    key: u32,
    chunk: u32,
    rows: &[Posting],
) -> Result<()> {
    if rows.is_empty() {
        tx.execute(
            "DELETE FROM gram_postings WHERE lane=?1 AND gram=?2 AND chunk=?3",
            params![lane, key, chunk],
        )?;
        return Ok(());
    }
    let (mut body, mut last, mut timestamp) = (b"R1".to_vec(), 0, 0i64);
    for row in rows {
        put_varint(row.block - last, &mut body);
        put_varint(row.frequency, &mut body);
        let delta = row.max_ts - timestamp;
        put_wide(
            (delta as u64).wrapping_shl(1) ^ ((delta >> 63) as u64),
            &mut body,
        );
        body.push(row.weight);
        last = row.block;
        timestamp = row.max_ts;
    }
    tx.prepare_cached("INSERT INTO gram_postings VALUES(?1,?2,?3,?4) ON CONFLICT(lane,gram,chunk) DO UPDATE SET body=excluded.body")?.execute(params![lane,key,chunk,lz4_flex::compress_prepend_size(&body)])?;
    Ok(())
}

pub(super) fn raise_changed_bounds(tx: &Transaction<'_>, changes: &mut Changes) -> Result<()> {
    for (table, dirty) in [("prose", "bound_updates"), ("tools", "tool_bound_updates")] {
        let mut query = tx.prepare_cached(&format!("SELECT id FROM {dirty}"))?;
        for id in query.query_map([], |r| r.get::<_, i64>(0))? {
            changes.touch(table, id?);
        }
    }
    let mut enqueue = tx.prepare_cached("INSERT OR IGNORE INTO bound_rebuild VALUES(?)")?;
    for block in &changes.blocks {
        enqueue.execute([block])?;
    }
    // Old contributions may disappear, but keeping their maxima cannot hide a hit.
    update_blocks(tx, &changes.blocks, Some(&changes.bounds))
}

pub(crate) fn tighten_bounds_step(root: &DataRoot, c: &mut Connection) -> Result<bool> {
    let _lock = root.lock_behind_writers()?;
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let block: Option<i64> = tx
        .query_row("SELECT block FROM bound_rebuild LIMIT 1", [], |r| r.get(0))
        .optional()?;
    let Some(block) = block else { return Ok(false) };
    let watch = root.clone();
    tx.progress_handler(1000, Some(move || watch.writer_queued().unwrap_or(true)));
    let result = update_blocks(&tx, &BTreeSet::from([block]), None);
    tx.progress_handler(0, None::<fn() -> bool>);
    if let Err(error) = result {
        if error.downcast_ref::<rusqlite::Error>().is_some_and(|e| matches!(e, rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::OperationInterrupted)) {
            return Ok(true);
        }
        return Err(error);
    }
    let (lane, id) = if block < 0 {
        (1, -1 - block)
    } else {
        (0, block)
    };
    tx.execute(
        "DELETE FROM gram_raises WHERE lane=?1 AND block=?2",
        params![lane, id],
    )?;
    tx.execute("DELETE FROM bound_rebuild WHERE block=?", [block])?;
    tx.commit()?;
    Ok(true)
}

struct PostingChange {
    rows: Vec<Posting>,
    dirty: bool,
}

pub(super) fn update_blocks(
    tx: &Transaction<'_>,
    blocks: &BTreeSet<i64>,
    delta: Option<&[BTreeSet<i64>; 2]>,
) -> Result<()> {
    let first: bool = !tx.query_row("SELECT EXISTS(SELECT 1 FROM gram_postings)", [], |r| {
        r.get::<_, bool>(0)
    })?;
    let mut changed: HashMap<(i64, u32, u32), PostingChange> = HashMap::new();
    let keys: Vec<_> = blocks.iter().copied().collect();
    for chunk in keys.chunks(32) {
        let mut input = vec![];
        for &key in chunk {
            let (table, block) = if key < 0 {
                ("tools", -1 - key)
            } else {
                ("prose", key)
            };
            let lane = usize::from(key < 0);
            let rows = read_bound_rows(tx, table, block, delta.map(|d| &d[lane]))?;
            if delta.is_some() && rows.is_empty() {
                continue;
            }
            let append = delta.is_some();
            input.push((key, rows, append));
        }
        let small =
            input.len() <= 1 || input.iter().map(|(_, rows, _)| rows.len()).sum::<usize>() <= 16;
        let build = |(key, rows, append)| {
            let mut bounds = build_bounds(key, rows)?;
            bounds.append = append;
            Ok::<_, anyhow::Error>(bounds)
        };
        let built: Vec<_> = if small {
            input.into_iter().map(build).collect()
        } else {
            input.into_par_iter().map(build).collect()
        };
        for b in built {
            write_bound(tx, b?, first, &mut changed)?;
        }
    }
    let mut changed: Vec<_> = changed.into_iter().collect();
    changed.sort_unstable_by_key(|a| a.0);
    for ((lane, gram, chunk), PostingChange { mut rows, dirty }) in changed {
        if !dirty {
            continue;
        }
        if first {
            rows.sort_unstable_by_key(|r| r.block);
        }
        write_posting_chunk(tx, lane, gram, chunk, &rows)?;
    }
    Ok(())
}

/// Recompute only the cheap positional separator bound for an isolated profile
/// of an existing index. Ordinary refresh updates it together with each block.
#[doc(hidden)]
pub fn refresh_phrase_gaps(root: &DataRoot) -> Result<usize> {
    let _work = root.work_lock(true)?;
    let _lock = root.lock(true)?;
    let mut c = writer(root)?;
    let tx = c.transaction()?;
    let mut blocks = HashMap::<i64, usize>::new();
    {
        let mut q = tx.prepare("SELECT id,text FROM tools WHERE visible=1 ORDER BY id")?;
        let mut rows = q.query([])?;
        while let Some(row) = rows.next()? {
            let id: i64 = row.get(0)?;
            let text = decode("tools", row.get(1)?)?;
            let gap = crate::fts_span::gap_upper(&search_fold(&text));
            blocks
                .entry(id / BLOCK)
                .and_modify(|n| *n = (*n).max(gap))
                .or_insert(gap);
        }
    }
    for (&id, &gap) in &blocks {
        tx.execute(
            "UPDATE tool_blocks SET max_gap=?2 WHERE id=?1",
            params![id, gap],
        )?;
    }
    tx.commit()?;
    Ok(blocks.len())
}

/// Rebuild private retrieval bounds on an isolated benchmark index.
#[doc(hidden)]
pub fn rebuild_bounds(root: &DataRoot) -> Result<usize> {
    let _work = root.work_lock(true)?;
    let _lock = root.lock(true)?;
    let mut c = writer(root)?;
    let tx = c.transaction()?;
    let mut blocks = BTreeSet::new();
    for (table, tools) in [("prose", false), ("tools", true)] {
        let mut query = tx.prepare(&format!(
            "SELECT DISTINCT id / ? FROM {table} WHERE visible=1"
        ))?;
        for block in query.query_map([BLOCK], |r| r.get::<_, i64>(0))? {
            let block = block?;
            blocks.insert(if tools { -1 - block } else { block });
        }
    }
    tx.execute_batch("DELETE FROM gram_postings; DELETE FROM block_grams; DELETE FROM blocks; DELETE FROM tool_blocks; DELETE FROM bound_rebuild; DELETE FROM gram_raises;")?;
    update_blocks(&tx, &blocks, None)?;
    tx.commit()?;
    Ok(blocks.len())
}
fn read_bound_rows(
    tx: &Transaction<'_>,
    table: &str,
    block: i64,
    delta: Option<&BTreeSet<i64>>,
) -> Result<Vec<BoundRow>> {
    Ok(if let Some(delta) = delta {
        let mut query = tx.prepare_cached(&format!(
            "SELECT text,ts,who,session,position FROM {table} WHERE visible=1 AND id>=?1 AND id<?2"
        ))?;
        let mut rows = Vec::new();
        let mut ids = delta
            .range(block * BLOCK..(block + 1) * BLOCK)
            .copied()
            .peekable();
        while let Some(lo) = ids.next() {
            let mut end = lo + 1;
            while ids.peek() == Some(&end) {
                ids.next();
                end += 1;
            }
            let mut selected = query.query(params![lo, end])?;
            while let Some(row) = selected.next()? {
                rows.push((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ));
            }
        }
        rows
    } else {
        tx.prepare_cached(&format!(
            "SELECT text,ts,who,session,position FROM {table} WHERE visible=1 AND id>=?1 AND id<?2"
        ))?
        .query_map(params![block * BLOCK, (block + 1) * BLOCK], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })?
        .collect::<rusqlite::Result<Vec<BoundRow>>>()?
    })
}

fn update_posting(
    tx: &Transaction<'_>,
    (lane, gram, id): (i64, u32, i64),
    new: &HashMap<u32, GramStat>,
    append: bool,
    first: bool,
    changed: &mut HashMap<(i64, u32, u32), PostingChange>,
) -> Result<()> {
    let b_append = append;
    let key = (lane, gram, id as u32 / POSTING_CHUNK);
    let PostingChange { rows, dirty } = match changed.entry(key) {
        std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
        std::collections::hash_map::Entry::Vacant(entry) => {
            let rows = if first {
                Vec::new()
            } else {
                posting_chunk(tx, lane, key.1, key.2)?
            };
            entry.insert(PostingChange { rows, dirty: false })
        }
    };
    if first {
        if let Some(tf) = new.get(&key.1) {
            *dirty = true;
            rows.push(Posting {
                block: id as u32,
                frequency: tf.frequency,
                max_ts: tf.max_ts,
                weight: tf.weight,
            });
        }
        return Ok(());
    }
    match rows.binary_search_by_key(&(id as u32), |p| p.block) {
        Ok(i) => {
            if let Some(tf) = new.get(&key.1) {
                let prior = rows[i];
                let next = Posting {
                    block: id as u32,
                    frequency: if b_append {
                        prior.frequency.max(tf.frequency)
                    } else {
                        tf.frequency
                    },
                    max_ts: if b_append {
                        prior.max_ts.max(tf.max_ts)
                    } else {
                        tf.max_ts
                    },
                    weight: if b_append {
                        prior.weight.max(tf.weight)
                    } else {
                        tf.weight
                    },
                };
                *dirty |= next != prior;
                rows[i] = next;
            } else {
                *dirty = true;
                rows.remove(i);
            }
        }
        Err(i) => {
            if let Some(tf) = new.get(&key.1) {
                *dirty = true;
                rows.insert(
                    i,
                    Posting {
                        block: id as u32,
                        frequency: tf.frequency,
                        max_ts: tf.max_ts,
                        weight: tf.weight,
                    },
                );
            }
        }
    }
    Ok(())
}

fn write_bound(
    tx: &Transaction<'_>,
    mut b: BuiltBounds,
    first: bool,
    changed: &mut HashMap<(i64, u32, u32), PostingChange>,
) -> Result<()> {
    if b.append && b.min_turn == i64::MAX {
        return Ok(());
    }
    let (lane, metadata, id) = if b.key < 0 {
        (1, "tool_blocks", -1 - b.key)
    } else {
        (0, "blocks", b.key)
    };
    let old: Option<Vec<u8>> = tx
        .query_row(
            "SELECT body FROM block_grams WHERE lane=?1 AND block=?2",
            params![lane, id],
            |r| r.get(0),
        )
        .optional()?;
    let mut presence =
        old.map_or_else(|| Ok(vec![0; GRAM_BITMAP_BYTES]), |body| gram_bitmap(&body))?;
    let old: Vec<_> = if b.append {
        Vec::new()
    } else {
        bitmap_grams(&presence).collect()
    };
    let new: HashMap<_, _> = b.grams.iter().cloned().collect();
    let grams: HashSet<_> = if b.append {
        new.keys().cloned().collect()
    } else {
        old.iter().cloned().chain(new.keys().cloned()).collect()
    };
    for gram in grams {
        if b.append {
            if let Some(tf) = new.get(&gram) {
                tx.prepare_cached("INSERT INTO gram_raises VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(lane,gram,block) DO UPDATE SET frequency=max(frequency,excluded.frequency),max_ts=max(max_ts,excluded.max_ts),weight=max(weight,excluded.weight) WHERE frequency<excluded.frequency OR max_ts<excluded.max_ts OR weight<excluded.weight")?.execute(params![lane,gram,id,tf.frequency,tf.max_ts,tf.weight])?;
            }
            continue;
        }

        update_posting(tx, (lane, gram, id), &new, b.append, first, changed)?;
    }
    if b.append {
        type PriorBound = (i64, Option<i64>, f64, String, i64, i64, bool);
        let prior: Option<PriorBound> = tx.query_row(&format!("SELECT max_ts,user_ts,weight,min_session,min_turn,max_gap,all_ascii FROM {metadata} WHERE id=?"),[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
        if let Some((ts, user_ts, weight, session, turn, gap, ascii)) = prior {
            b.max_ts = b.max_ts.max(ts);
            b.user_ts = b.user_ts.max(user_ts);
            b.weight = b.weight.max(weight);
            b.max_gap = b.max_gap.max(gap as usize);
            b.all_ascii &= ascii;
            if session < b.min_session {
                b.min_session = session;
                b.min_turn = turn;
            } else if session == b.min_session {
                b.min_turn = b.min_turn.min(turn);
            }
        }
    }
    let packed = if b.append {
        for &(key, _) in &b.grams {
            presence[key as usize / 8] |= 1 << (key % 8);
        }
        lz4_flex::compress_prepend_size(&presence)
    } else {
        pack_grams(&b.grams)
    };
    tx.prepare_cached("INSERT INTO block_grams VALUES(?1,?2,?3) ON CONFLICT(lane,block) DO UPDATE SET body=excluded.body")?.execute(params![lane,id,packed])?;
    tx.execute(&format!("DELETE FROM {metadata} WHERE id=?"), [id])?;
    if b.min_turn != i64::MAX {
        tx.prepare_cached(&format!(
            "INSERT INTO {metadata} VALUES(?1,?2,?3,?4,?5,?6,?7,?8)"
        ))?
        .execute(params![
            id,
            b.max_ts,
            b.user_ts,
            b.weight,
            b.min_session,
            b.min_turn,
            b.max_gap as i64,
            b.all_ascii
        ])?;
    }

    Ok(())
}

#[cfg(test)]
mod bound_tests {
    #[test]
    fn integer_bound_keys_and_postings_round_trip() {
        for code in 0..47988 {
            assert_eq!(
                super::gram_code(&super::gram_text(code).unwrap()),
                Some(code)
            );
        }
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        let rows = [
            (0, 1, 0, 0),
            (127, 4, 1_700_000_000_123, 6),
            (128, 130, 1, 3),
            (65_000, 2, i64::MAX, 5),
            (u32::MAX, 30, 0, 1),
        ]
        .map(|(block, frequency, max_ts, weight)| super::Posting {
            block,
            frequency,
            max_ts,
            weight,
        })
        .to_vec();
        super::write_posting(&tx, 0, "abc", &rows).unwrap();
        assert_eq!(super::posting_list(&tx, 0, "abc").unwrap(), rows);
        let grams = ["a", "00", "zzz"]
            .map(|s| {
                (
                    super::gram_code(s).unwrap(),
                    super::GramStat {
                        frequency: 1,
                        ..Default::default()
                    },
                )
            })
            .to_vec();
        let mut got: Vec<_> = super::unpack_grams(&super::pack_grams(&grams))
            .unwrap()
            .into_iter()
            .map(|code| super::gram_text(code).unwrap())
            .collect();
        got.sort();
        assert_eq!(got, vec!["00", "a", "zzz"]);
    }
    #[test]
    fn raised_postings_merge_in_block_order_and_keep_each_maximum() {
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        let base = [
            super::Posting {
                block: 0,
                frequency: 4,
                max_ts: 200,
                weight: 3,
            },
            super::Posting {
                block: 128,
                frequency: 1,
                max_ts: 500,
                weight: 6,
            },
        ];
        super::write_posting(&tx, 0, "a", &base).unwrap();
        let gram = super::gram_code("a").unwrap();
        for (block, frequency, ts, weight) in [(0, 3, 300, 6), (1, 2, 600, 0)] {
            tx.execute(
                "INSERT INTO gram_raises VALUES(0,?1,?2,?3,?4,?5)",
                rusqlite::params![gram, block, frequency, ts, weight],
            )
            .unwrap();
        }
        tx.execute_batch("PRAGMA reverse_unordered_selects=ON")
            .unwrap();
        assert_eq!(
            super::posting_list(&tx, 0, "a").unwrap(),
            vec![
                super::Posting {
                    block: 0,
                    frequency: 4,
                    max_ts: 300,
                    weight: 6
                },
                super::Posting {
                    block: 1,
                    frequency: 2,
                    max_ts: 600,
                    weight: 0
                },
                base[1],
            ]
        );
        tx.execute("INSERT INTO gram_raises VALUES(1,?1,8,2,100,0)", [gram])
            .unwrap();
        assert_eq!(
            super::posting_list(&tx, 1, "a").unwrap(),
            vec![super::Posting {
                block: 8,
                frequency: 2,
                max_ts: 100,
                weight: 0
            }]
        );
    }

    fn bound_row(text: &str, turn: i64, ts: i64) -> super::NewRow<'_> {
        super::NewRow {
            agent: "pi",
            session: "s",
            project: "p",
            turn,
            who: "user",
            model: "",
            model_source: "unknown",
            ts,
            text,
            extra: String::new(),
            bulk: false,
        }
    }

    #[test]
    fn raising_bounds_reads_only_changed_rows_without_rewriting_base_postings() {
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        let mut changes = super::Changes::default();
        super::upsert(&tx, "prose", 1, bound_row("aaaa", 0, 200), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("old", 1, 100), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("ccc", 2, 200), &mut changes).unwrap();
        super::update_blocks(&tx, &changes.blocks, None).unwrap();
        tx.commit().unwrap();
        // An unrelated row must never be decoded to raise one changed row's bounds.
        c.execute("UPDATE prose SET text=x'ff' WHERE turn=1", [])
            .unwrap();
        c.execute("DELETE FROM bound_updates", []).unwrap();
        c.execute_batch("CREATE TRIGGER reject_unchanged_posting BEFORE INSERT ON gram_postings BEGIN SELECT RAISE(FAIL,'unchanged posting write'); END;").unwrap();
        let tx = c.transaction().unwrap();
        let mut changes = super::Changes::default();
        super::upsert(&tx, "prose", 1, bound_row("aaa", 0, 100), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("c", 2, 100), &mut changes).unwrap();
        super::raise_changed_bounds(&tx, &mut changes).unwrap();
        let a = super::posting_list(&tx, 0, "a").unwrap();
        assert_eq!((a[0].frequency, a[0].max_ts), (4, 200));
        tx.execute_batch("DROP TRIGGER reject_unchanged_posting")
            .unwrap();
        assert_eq!(
            tx.query_row("SELECT count(*) FROM bound_rebuild", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
        super::upsert(&tx, "prose", 1, bound_row("bbbb Ω", 0, 300), &mut changes).unwrap();
        super::raise_changed_bounds(&tx, &mut changes).unwrap();
        let b = super::posting_list(&tx, 0, "b").unwrap();
        assert_eq!((b[0].frequency, b[0].max_ts), (4, 300));
        assert_eq!(super::posting_list(&tx, 0, "a").unwrap(), a);
        assert!(!tx
            .query_row("SELECT all_ascii FROM blocks WHERE id=0", [], |r| r
                .get::<_, bool>(0))
            .unwrap());
    }

    #[test]
    fn background_tightening_removes_deleted_contributions() {
        let temp = tempfile::tempdir().unwrap();
        let root = crate::data::DataRoot::open(temp.path().join("data"), true).unwrap();
        let mut c = super::writer(&root).unwrap();
        let tx = c.transaction().unwrap();
        let mut changes = super::Changes::default();
        super::upsert(&tx, "prose", 1, bound_row("aaaa", 0, 200), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("bbb", 1, 100), &mut changes).unwrap();
        super::update_blocks(&tx, &changes.blocks, None).unwrap();
        let mut changes = super::Changes::default();
        super::erase(&tx, "prose", 1, &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("bbbb", 1, 300), &mut changes).unwrap();
        super::raise_changed_bounds(&tx, &mut changes).unwrap();
        assert_eq!(super::posting_list(&tx, 0, "a").unwrap()[0].frequency, 4);
        tx.commit().unwrap();
        assert!(super::tighten_bounds_step(&root, &mut c).unwrap());
        assert!(!super::tighten_bounds_step(&root, &mut c).unwrap());
        assert!(super::posting_list(&c, 0, "a").unwrap().is_empty());
        assert_eq!(super::posting_list(&c, 0, "b").unwrap()[0].frequency, 4);
        assert_eq!(
            c.query_row("SELECT count(*) FROM gram_raises", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            c.query_row("SELECT max_ts FROM blocks WHERE id=0", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            300
        );
    }

    #[test]
    fn counts_are_per_row_not_summed_across_a_block() {
        let got = super::gram_maxima(
            ["aaaa", "aa", "ééé"]
                .into_iter()
                .map(|s| (s.to_owned(), 1, "user".to_owned())),
        );
        assert_eq!(got[&super::gram_code("a").unwrap()].frequency, 4);
        assert_eq!(got[&super::gram_code("aa").unwrap()].frequency, 3);
        assert_eq!(got[&super::gram_code("aaa").unwrap()].frequency, 2);
        assert_eq!(got.len(), 3);
    }
    #[test]
    fn dense_counts_match_literal_grams_across_unicode_and_punctuation() {
        let rows = [
            "abc abc0 000 zzzzzéabc a🦀bc １２３xyz",
            "bcabc a_b-a.b a\nabc 0123456789abcdefghijklmnopqrstuvwxyz",
            "é界Σ🦀",
            "9za9za9za",
        ];
        let mut expected = std::collections::HashMap::<String, u32>::new();
        for text in rows {
            for (gram, count) in super::grams(text) {
                if super::indexed_gram(&gram) {
                    expected
                        .entry(gram)
                        .and_modify(|max| *max = (*max).max(count))
                        .or_insert(count);
                }
            }
        }
        assert_eq!(
            super::gram_maxima(
                rows.into_iter()
                    .map(|s| (s.to_owned(), 1, "user".to_owned()))
            )
            .into_iter()
            .map(|(code, v)| (super::gram_text(code).unwrap(), v.frequency))
            .collect::<std::collections::HashMap<_, _>>(),
            expected
        );
    }
}
