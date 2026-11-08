use super::text::{decode, search_key};
use super::WriteTimer;
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::{params, Transaction};
use std::collections::BTreeMap;

type FtsInput = Vec<(i64, Vec<u8>)>;
type FtsPrepared = Vec<(i64, String, bool)>;
type FtsWords = Vec<(i64, Vec<Box<[u8]>>)>;
/// A prepared row: its id, indexed text and distinct dictionary words.
type FtsRow = (i64, String, Vec<Box<[u8]>>, bool);
fn read_fts_batch(rows: &mut rusqlite::Rows<'_>) -> Result<FtsInput> {
    let mut input = Vec::with_capacity(1024);
    for _ in 0..1024 {
        let Some(row) = rows.next()? else { break };
        input.push((row.get(0)?, row.get(1)?));
    }
    Ok(input)
}
fn prepare_fts_batch(
    table: &str,
    input: FtsInput,
    tokenizers: Option<&crate::tool_index::Tokenizers<'_>>,
) -> Result<(FtsPrepared, FtsWords)> {
    let rows: Vec<FtsRow> = input
        .into_par_iter()
        .map_init(
            || tokenizers.map(|t| t.tokenizer()),
            |tokenizer, (id, bytes)| {
                let (text, aliases) = search_key(&decode(table, bytes)?);
                if table != "tools" {
                    return Ok((id, text, Vec::new(), aliases));
                }
                let text = crate::tool_index::index_text(&text).into_owned();
                let words = match tokenizer {
                    Some(Ok(tokenizer)) => crate::tool_index::row_words(tokenizer, &text)?,
                    Some(Err(error)) => anyhow::bail!("{error:#}"),
                    None => Vec::new(),
                };
                Ok((id, text, words, aliases))
            },
        )
        .collect::<Result<_>>()?;
    let mut prepared = Vec::with_capacity(rows.len());
    let mut words = Vec::with_capacity(rows.len());
    for (id, text, row_words, aliases) in rows {
        prepared.push((id, text, aliases));
        words.push((id, row_words));
    }
    Ok((prepared, words))
}
/// Returns tool word presence, gathered beside the inserts from the same text.
pub(super) fn build_fts(tx: &Transaction<'_>) -> Result<Option<crate::tool_index::Presence>> {
    let _timer = WriteTimer::new(true);
    let tokenizers = crate::tool_index::Tokenizers::new(tx)?;
    let mut collected = None;
    for table in ["prose", "tools"] {
        tx.execute_batch(&format!("INSERT INTO {table}_fts({table}_fts,rank) VALUES('automerge',0);INSERT INTO {table}_fts({table}_fts,rank) VALUES('crisismerge',64);INSERT INTO {table}_fts({table}_fts,rank) VALUES('hashsize',16777216);"))?;
        let mut source = tx.prepare(&format!(
            "SELECT id,text FROM {table} WHERE visible=1 ORDER BY id"
        ))?;
        let mut rows = source.query([])?;
        // SQLite remains exclusively on this thread. Two bounded batches let
        // decode/normalization overlap inserts, without a second writer or an
        // unbounded copy of the corpus.
        std::thread::scope(|scope| -> Result<()> {
            let (send, input) = std::sync::mpsc::sync_channel::<FtsInput>(1);
            let (output, receive) = std::sync::mpsc::sync_channel::<Result<FtsPrepared>>(1);
            let tokenizers = (table == "tools").then_some(&tokenizers);
            // Word presence is merged here, after the batch is handed to the
            // writer, so it overlaps the FTS inserts.
            let worker = scope.spawn(move || -> Result<Option<crate::tool_index::Presence>> {
                let mut presence = tokenizers.map(|_| crate::tool_index::new_presence());
                while let Ok(batch) = input.recv() {
                    match prepare_fts_batch(table, batch, tokenizers) {
                        Ok((prepared, words)) => {
                            if output.send(Ok(prepared)).is_err() {
                                break;
                            }
                            if let Some(presence) = presence.as_mut() {
                                crate::tool_index::add_rows(presence, words)?;
                            }
                        }
                        Err(error) => {
                            let _ = output.send(Err(error));
                            break;
                        }
                    }
                }
                Ok(presence)
            });
            let result = (|| -> Result<()> {
                let mut queued = 0;
                let mut ended = false;
                for _ in 0..2 {
                    let batch = read_fts_batch(&mut rows)?;
                    if batch.is_empty() {
                        ended = true;
                        break;
                    }
                    send.send(batch)
                        .map_err(|_| anyhow::anyhow!("FTS preparation ended"))?;
                    queued += 1;
                }
                while queued > 0 {
                    let prepared = receive
                        .recv()
                        .map_err(|_| anyhow::anyhow!("FTS preparation ended"))??;
                    queued -= 1;
                    if !ended {
                        let batch = read_fts_batch(&mut rows)?;
                        if batch.is_empty() {
                            ended = true;
                        } else {
                            send.send(batch)
                                .map_err(|_| anyhow::anyhow!("FTS preparation ended"))?;
                            queued += 1;
                        }
                    }
                    // A multi-row statement opens a savepoint, and FTS5 flushes its
                    // pending terms to a new segment at every savepoint.
                    let mut insert = tx.prepare_cached(&format!(
                        "INSERT INTO {table}_fts(rowid,text) VALUES(?1,?2)"
                    ))?;
                    let mut mark = tx.prepare_cached(&format!(
                        "INSERT INTO {table}_case_fts(rowid,text) VALUES(?1,?2)"
                    ))?;
                    for (id, text, aliases) in &prepared {
                        insert.execute(params![id, text])?;
                        if *aliases {
                            mark.execute(params![id, text])?;
                        }
                    }
                }
                Ok(())
            })();
            drop(send);
            drop(receive);
            let presence = worker
                .join()
                .map_err(|_| anyhow::anyhow!("FTS preparation panicked"))??;
            result?;
            if presence.is_some() {
                collected = presence;
            }
            Ok(())
        })?;
        tx.execute(
            &format!("INSERT INTO {table}_fts({table}_fts) VALUES('optimize')"),
            [],
        )?;
        tx.execute(
            &format!("INSERT INTO {table}_case_fts({table}_case_fts) VALUES('optimize')"),
            [],
        )?;
        crate::fts_segments::configure(tx, &format!("{table}_fts"))?;
    }
    Ok(collected)
}
/// What one transaction's row writes leave for derived structures to catch up on.
/// The FTS rows a transaction touched, per lane: whether each was indexed when the
/// transaction began and whether it is indexed at its end.
///
/// FTS5 flushes its pending terms into a new segment whenever a write's rowid is not
/// above the previous write's, and at every statement savepoint, which each row
/// statement with a trigger opens. Writing the net change once, in rowid order and
/// after the row statements, leaves one new segment per table.
#[derive(Default)]
pub(super) struct FtsWrites([BTreeMap<i64, (bool, bool)>; 2]);
impl FtsWrites {
    pub(super) fn insert(&mut self, table: &str, id: i64) {
        self.0[usize::from(table == "tools")]
            .entry(id)
            .or_insert((false, false))
            .1 = true;
    }
    pub(super) fn delete(&mut self, table: &str, id: i64) {
        self.0[usize::from(table == "tools")]
            .entry(id)
            .or_insert((true, false))
            .1 = false;
    }
    /// Indexed text is read back from the rows, as the first build reads it.
    pub(super) fn apply(&mut self, tx: &Transaction<'_>) -> Result<()> {
        let _timer = WriteTimer::new(true);
        for (table, rows) in ["prose", "tools"].into_iter().zip(&mut self.0) {
            let mut delete =
                tx.prepare_cached(&format!("DELETE FROM {table}_fts WHERE rowid=?"))?;
            let mut insert = tx.prepare_cached(&format!(
                "INSERT INTO {table}_fts(rowid,text) VALUES(?1,?2)"
            ))?;
            let mut text = tx.prepare_cached(&format!("SELECT text FROM {table} WHERE id=?"))?;
            for (id, (before, after)) in std::mem::take(rows) {
                if before {
                    delete.execute([id])?;
                    tx.execute(&format!("DELETE FROM {table}_case_fts WHERE rowid=?"), [id])?;
                }
                if after {
                    let (normalized, aliases) =
                        search_key(&decode(table, text.query_row([id], |r| r.get(0))?)?);
                    if aliases {
                        tx.execute(
                            &format!("INSERT INTO {table}_case_fts(rowid,text) VALUES(?1,?2)"),
                            params![
                                id,
                                if table == "tools" {
                                    crate::tool_index::index_text(&normalized)
                                } else {
                                    std::borrow::Cow::Borrowed(normalized.as_str())
                                }
                            ],
                        )?;
                    }
                    if table == "tools" {
                        insert.execute(params![
                            id,
                            crate::tool_index::index_text(&normalized).as_ref()
                        ])?;
                    } else {
                        insert.execute(params![id, normalized])?;
                    }
                }
            }
        }
        Ok(())
    }
}
