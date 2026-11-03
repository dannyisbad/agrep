//! Exact trigram document frequencies, independent of FTS posting traversal.
use crate::store;
use anyhow::Result;
use rusqlite::{params, Connection, Transaction};
use std::collections::{HashMap, HashSet};

pub(crate) fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS prose_terms(term TEXT PRIMARY KEY,doc INTEGER NOT NULL) WITHOUT ROWID;")?;
    Ok(())
}

fn terms(text: &str) -> HashSet<String> {
    let normal = store::normalize(text);
    normal
        .chars()
        .collect::<Vec<_>>()
        .windows(3)
        .map(|g| g.iter().collect())
        .collect()
}

pub(crate) fn refresh(tx: &Transaction<'_>, first: bool) -> Result<()> {
    if first {
        tx.execute(
            "INSERT INTO prose_terms SELECT term,doc FROM prose_vocab",
            [],
        )?;
        return Ok(());
    }
    // stats_dirty captures the old visible text before any row or family change.
    // Count each trigram once in the normal-text FTS field, exactly as fts5vocab(row).
    let mut delta: HashMap<String, i64> = HashMap::new();
    let mut old = tx.prepare("SELECT old_text FROM stats_dirty WHERE old_text IS NOT NULL")?;
    for row in old.query_map([], |r| r.get::<_, Vec<u8>>(0))? {
        for term in terms(&String::from_utf8(row?)?) {
            *delta.entry(term).or_default() -= 1;
        }
    }
    let mut new = tx.prepare(
        "SELECT p.text FROM stats_dirty d CROSS JOIN prose p ON p.id=d.id WHERE p.visible=1",
    )?;
    for row in new.query_map([], |r| r.get::<_, Vec<u8>>(0))? {
        for term in terms(&String::from_utf8(row?)?) {
            *delta.entry(term).or_default() += 1;
        }
    }
    let mut update = tx.prepare_cached("INSERT INTO prose_terms VALUES(?1,?2) ON CONFLICT(term) DO UPDATE SET doc=doc+excluded.doc")?;
    for (term, n) in delta {
        if n != 0 {
            update.execute(params![term, n])?;
            if n < 0 {
                tx.execute("DELETE FROM prose_terms WHERE term=? AND doc<=0", [&term])?;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frequencies_equal_fts_after_insert_rewrite_and_delete() {
        let mut c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE prose(id INTEGER PRIMARY KEY,text BLOB,visible INTEGER);
CREATE TABLE stats_dirty(id INTEGER PRIMARY KEY,old_text BLOB);
CREATE VIRTUAL TABLE prose_fts USING fts5(text,content='',contentless_delete=1,tokenize='trigram case_sensitive 1');
CREATE VIRTUAL TABLE prose_vocab USING fts5vocab(prose_fts,row);").unwrap();
        schema(&c).unwrap();
        for (text, first) in [
            (Some("Ab—cd abcd é界"), true),
            (Some("Other é界 text"), false),
            (None, false),
        ] {
            let tx = c.transaction().unwrap();
            tx.execute(
                "INSERT INTO stats_dirty SELECT id,text FROM prose WHERE visible=1",
                [],
            )
            .unwrap();
            tx.execute("DELETE FROM prose_fts", []).unwrap();
            tx.execute("DELETE FROM prose", []).unwrap();
            if let Some(text) = text {
                let normal = store::normalize(text);
                tx.execute("INSERT INTO prose VALUES(1,?,1)", [text.as_bytes()])
                    .unwrap();
                tx.execute("INSERT OR IGNORE INTO stats_dirty VALUES(1,NULL)", [])
                    .unwrap();
                tx.execute(
                    "INSERT INTO prose_fts(rowid,text) VALUES(1,?1)",
                    params![normal],
                )
                .unwrap();
            }
            refresh(&tx, first).unwrap();
            let expected: Vec<(String, i64)> = tx
                .prepare("SELECT term,doc FROM prose_vocab ORDER BY term")
                .unwrap()
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap();
            let got: Vec<(String, i64)> = tx
                .prepare("SELECT term,doc FROM prose_terms ORDER BY term")
                .unwrap()
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap();
            assert_eq!(got, expected);
            tx.execute("DELETE FROM stats_dirty", []).unwrap();
            tx.commit().unwrap();
        }
    }
}
