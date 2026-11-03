//! Positional word postings plus a trigram dictionary preserve ASCII substring matches.
//! The dictionary expands substrings to complete indexed terms; row text is not needed.
use crate::store;
use anyhow::Result;
use rusqlite::{Connection, Transaction};

pub(crate) fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS tool_words(id INTEGER PRIMARY KEY,term TEXT NOT NULL UNIQUE,doc INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS tool_lexicon USING fts5(term,content='',contentless_delete=1,tokenize='trigram case_sensitive 1',detail=none);
CREATE TRIGGER IF NOT EXISTS tool_word_insert AFTER INSERT ON tool_words BEGIN INSERT INTO tool_lexicon(rowid,term) VALUES(new.id,new.term); END;
CREATE TABLE IF NOT EXISTS tool_word_dirty(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS tool_word_old(id INTEGER PRIMARY KEY,text BLOB NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS tool_word_batch USING fts5(text,content='',contentless_delete=1,tokenize='unicode61 remove_diacritics 0',detail=none);
CREATE VIRTUAL TABLE IF NOT EXISTS tool_word_batch_vocab USING fts5vocab(tool_word_batch,row);")?;
    let compact: bool = c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_rows')",
        [],
        |r| r.get(0),
    )?;
    c.execute_batch(if compact {
        "CREATE TRIGGER IF NOT EXISTS tool_word_old_update BEFORE UPDATE OF blob,visible,active ON event_rows WHEN old.blob IS NOT NULL AND old.visible=1 AND old.active=1 AND (old.blob IS NOT new.blob OR old.visible!=new.visible OR old.active!=new.active) BEGIN INSERT OR IGNORE INTO tool_word_old SELECT old.tool_id,body FROM tool_blobs WHERE id=old.blob; END;
CREATE TRIGGER IF NOT EXISTS tool_word_old_delete BEFORE DELETE ON event_rows WHEN old.blob IS NOT NULL AND old.visible=1 AND old.active=1 BEGIN INSERT OR IGNORE INTO tool_word_old SELECT old.tool_id,body FROM tool_blobs WHERE id=old.blob; END;
CREATE TRIGGER IF NOT EXISTS tool_word_dirty_insert AFTER INSERT ON event_rows WHEN new.blob IS NOT NULL AND new.visible=1 AND new.active=1 BEGIN INSERT OR IGNORE INTO tool_word_dirty VALUES(new.tool_id); END;
CREATE TRIGGER IF NOT EXISTS tool_word_dirty_update AFTER UPDATE OF blob,visible,active ON event_rows WHEN new.blob IS NOT NULL AND new.visible=1 AND new.active=1 AND (old.blob IS NOT new.blob OR old.visible!=new.visible OR old.active!=new.active) BEGIN INSERT OR IGNORE INTO tool_word_dirty VALUES(new.tool_id); END;"
    } else {
        "CREATE TRIGGER IF NOT EXISTS tool_word_old_update BEFORE UPDATE OF text,visible ON tools WHEN old.visible=1 BEGIN INSERT OR IGNORE INTO tool_word_old VALUES(old.id,old.text); END;
CREATE TRIGGER IF NOT EXISTS tool_word_old_delete BEFORE DELETE ON tools WHEN old.visible=1 BEGIN INSERT OR IGNORE INTO tool_word_old VALUES(old.id,old.text); END;
CREATE TRIGGER IF NOT EXISTS tool_word_dirty_insert AFTER INSERT ON tools WHEN new.visible=1 BEGIN INSERT OR IGNORE INTO tool_word_dirty VALUES(new.id); END;
CREATE TRIGGER IF NOT EXISTS tool_word_dirty_update AFTER UPDATE OF text,visible ON tools WHEN new.visible=1 BEGIN INSERT OR IGNORE INTO tool_word_dirty VALUES(new.id); END;"
    })?;
    Ok(())
}
pub(crate) fn refresh(tx: &Transaction<'_>, first: bool) -> Result<()> {
    if !first
        && !tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM tool_word_dirty UNION ALL SELECT 1 FROM tool_word_old)",
            [],
            |r| r.get::<_, bool>(0),
        )?
    {
        return Ok(());
    }
    if first {
        tx.execute_batch("INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('automerge',0);INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('crisismerge',64);INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('hashsize',16777216);")?;
        tx.execute("INSERT INTO tool_words(term,doc) SELECT term,doc FROM tools_vocab WHERE length(term)>=3 ON CONFLICT(term) DO UPDATE SET doc=excluded.doc", [])?;
        tx.execute(
            "INSERT INTO tool_lexicon(tool_lexicon) VALUES('optimize')",
            [],
        )?;
        tx.execute_batch("INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('automerge',4);INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('crisismerge',16);INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('hashsize',1048576);")?;
    } else {
        let mut q = tx.prepare(
            "SELECT p.text FROM tool_word_dirty d JOIN tools p ON p.id=d.id WHERE p.visible=1 UNION ALL SELECT text FROM tool_word_old",
        )?;
        let mut rows = q.query([])?;
        let mut insert = tx.prepare("INSERT INTO tool_word_batch(text) VALUES(?)")?;
        while let Some(row) = rows.next()? {
            let text = store::normalize(&store::decode("tools", row.get(0)?)?);
            insert.execute([index_text(&text).as_ref()])?;
        }
        tx.execute("INSERT INTO tool_words(term,doc) SELECT b.term,coalesce((SELECT doc FROM tools_vocab WHERE term=b.term),0) FROM tool_word_batch_vocab b WHERE length(b.term)>=3 ON CONFLICT(term) DO UPDATE SET doc=excluded.doc", [])?;
        tx.execute("DELETE FROM tool_word_batch", [])?;
    }
    tx.execute("DELETE FROM tool_word_dirty", [])?;
    tx.execute("DELETE FROM tool_word_old", [])?;
    Ok(())
}
/// None means the literal may span tokenizer boundaries and must use verified block scanning.
pub(crate) fn expand(c: &Connection, term: &str) -> Result<Option<Vec<(String, usize)>>> {
    if term.len() < 3 || term.len() > 4096 || !term.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return Ok(None);
    }
    let expression = term
        .as_bytes()
        .windows(3)
        .map(|g| format!("\"{}\"", std::str::from_utf8(g).unwrap()))
        .collect::<Vec<_>>()
        .join(" AND ");
    let mut q=c.prepare("SELECT w.term,w.doc FROM tool_lexicon l CROSS JOIN tool_words w ON w.id=l.rowid WHERE tool_lexicon MATCH ?1 AND instr(w.term,?2)>0 AND w.doc>0")?;
    let rows = q
        .query_map([&expression, term], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(Some(rows))
}

/// SQLite truncates tokens at 32 KiB. Overlapping 8 KiB windows preserve every
/// ASCII literal up to 4 KiB; longer literals use the verified scanning path.
pub(crate) fn index_text(text: &str) -> std::borrow::Cow<'_, str> {
    if !text.split(char::is_whitespace).any(|s| s.len() > 8192) {
        return std::borrow::Cow::Borrowed(text);
    }
    let mut out = String::with_capacity(text.len());
    for segment in text.split_inclusive(char::is_whitespace) {
        let word = segment.trim_end_matches(char::is_whitespace);
        if word.len() <= 8192 {
            out.push_str(segment);
            continue;
        }
        let mut start = 0;
        loop {
            let mut end = (start + 8192).min(word.len());
            while !word.is_char_boundary(end) {
                end -= 1;
            }
            out.push_str(&word[start..end]);
            if end == word.len() {
                break;
            }
            out.push(' ');
            start = end - 4096;
            while !word.is_char_boundary(start) {
                start -= 1;
            }
        }
        out.push_str(&segment[word.len()..]);
    }
    std::borrow::Cow::Owned(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dictionary_keeps_substrings_past_sqlite_token_limit() {
        let mut c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE tools(id INTEGER PRIMARY KEY,text BLOB,visible INTEGER);CREATE VIRTUAL TABLE tools_fts USING fts5(text,content='',tokenize='unicode61 remove_diacritics 0');CREATE VIRTUAL TABLE tools_vocab USING fts5vocab(tools_fts,row);").unwrap();
        schema(&c).unwrap();
        let text = format!("{}needle{}", "x".repeat(40_000), "y".repeat(40_000));
        let tx = c.transaction().unwrap();
        tx.execute(
            "INSERT INTO tools VALUES(1,?,1)",
            [store::encode_tool(&text).unwrap()],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO tools_fts(rowid,text) VALUES(1,?)",
            [index_text(&text).as_ref()],
        )
        .unwrap();
        refresh(&tx, true).unwrap();
        tx.commit().unwrap();
        let words = expand(&c, "needle").unwrap().unwrap();
        assert!(!words.is_empty());
        let expression = words
            .iter()
            .map(|(word, _)| format!("\"{word}\""))
            .collect::<Vec<_>>()
            .join(" OR ");
        assert_eq!(
            c.query_row(
                "SELECT count(*) FROM tools_fts WHERE tools_fts MATCH ?",
                [expression],
                |r| r.get::<_, usize>(0)
            )
            .unwrap(),
            1
        );
        assert!(expand(&c, &"x".repeat(4097)).unwrap().is_none());
    }
}

#[cfg(test)]
mod frequency_tests {
    use super::*;
    #[test]
    fn cached_frequency_tracks_deletion_and_reintroduction() {
        let mut c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE tools(id INTEGER PRIMARY KEY,text BLOB,visible INTEGER);CREATE VIRTUAL TABLE tools_fts USING fts5(text,content='',contentless_delete=1,tokenize='unicode61 remove_diacritics 0');CREATE VIRTUAL TABLE tools_vocab USING fts5vocab(tools_fts,row);").unwrap();
        schema(&c).unwrap();
        for (id, first) in [(1, true), (2, false)] {
            let tx = c.transaction().unwrap();
            tx.execute(
                "INSERT INTO tools VALUES(?1,?2,1)",
                rusqlite::params![id, store::encode_tool("needle").unwrap()],
            )
            .unwrap();
            tx.execute(
                "INSERT INTO tools_fts(rowid,text) VALUES(?1,'needle')",
                [id],
            )
            .unwrap();
            refresh(&tx, first).unwrap();
            tx.commit().unwrap();
            assert_eq!(
                expand(&c, "needle").unwrap().unwrap(),
                vec![("needle".into(), 1)]
            );
            let tx = c.transaction().unwrap();
            tx.execute("DELETE FROM tools_fts WHERE rowid=?", [id])
                .unwrap();
            tx.execute("DELETE FROM tools WHERE id=?", [id]).unwrap();
            refresh(&tx, false).unwrap();
            tx.commit().unwrap();
            assert!(expand(&c, "needle").unwrap().unwrap().is_empty());
        }
    }
}
