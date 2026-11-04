//! Positional word postings plus a trigram dictionary preserve ASCII substring matches.
//! The dictionary expands substrings to complete indexed terms; row text is not needed.
//! Each word also lists the 512-row tool blocks containing it, taken from the same
//! unicode61 tokens FTS5 stores, so block plans can leave absent words out.
use crate::store;
use anyhow::{bail, ensure, Result};
use rayon::prelude::*;
use rusqlite::{ffi, params, Connection, OptionalExtension, Transaction};
use std::{
    collections::{HashMap, HashSet},
    ffi::{c_char, c_int, c_void},
    marker::PhantomData,
    ptr,
};

pub(crate) fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS tool_words(id INTEGER PRIMARY KEY,term TEXT NOT NULL UNIQUE,doc INTEGER NOT NULL,blocks BLOB NOT NULL DEFAULT x'');
CREATE VIRTUAL TABLE IF NOT EXISTS tool_lexicon USING fts5(term,content='',contentless_delete=1,tokenize='trigram case_sensitive 1',detail=none);
CREATE TRIGGER IF NOT EXISTS tool_word_insert AFTER INSERT ON tool_words BEGIN INSERT INTO tool_lexicon(rowid,term) VALUES(new.id,new.term); END;
CREATE TABLE IF NOT EXISTS tool_word_dirty(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS tool_word_old(id INTEGER PRIMARY KEY,text BLOB NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS tool_word_batch USING fts5(text,content='',contentless_delete=1,tokenize='unicode61 remove_diacritics 0',detail=none);
CREATE VIRTUAL TABLE IF NOT EXISTS tool_word_batch_vocab USING fts5vocab(tool_word_batch,row);
CREATE TABLE IF NOT EXISTS tool_trigrams(gram INTEGER PRIMARY KEY,doc INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS tool_word_old_update BEFORE UPDATE OF blob,visible,active ON event_rows WHEN old.blob IS NOT NULL AND old.visible=1 AND old.active=1 AND (old.blob IS NOT new.blob OR old.visible!=new.visible OR old.active!=new.active) BEGIN INSERT OR IGNORE INTO tool_word_old SELECT old.tool_id,body FROM tool_blobs WHERE id=old.blob; END;
CREATE TRIGGER IF NOT EXISTS tool_word_old_delete BEFORE DELETE ON event_rows WHEN old.blob IS NOT NULL AND old.visible=1 AND old.active=1 BEGIN INSERT OR IGNORE INTO tool_word_old SELECT old.tool_id,body FROM tool_blobs WHERE id=old.blob; END;
CREATE TRIGGER IF NOT EXISTS tool_word_dirty_insert AFTER INSERT ON event_rows WHEN new.blob IS NOT NULL AND new.visible=1 AND new.active=1 BEGIN INSERT OR IGNORE INTO tool_word_dirty VALUES(new.tool_id); END;
CREATE TRIGGER IF NOT EXISTS tool_word_dirty_update AFTER UPDATE OF blob,visible,active ON event_rows WHEN new.blob IS NOT NULL AND new.visible=1 AND new.active=1 AND (old.blob IS NOT new.blob OR old.visible!=new.visible OR old.active!=new.active) BEGIN INSERT OR IGNORE INTO tool_word_dirty VALUES(new.tool_id); END;")?;
    Ok(())
}
/// The unicode61 tokenizer module that `tools_fts` uses, borrowed from a connection.
pub(crate) struct Tokenizers<'c> {
    module: ffi::fts5_tokenizer,
    user: *mut c_void,
    _connection: PhantomData<&'c Connection>,
}
// Creating a unicode61 instance only reads its immutable module data, and each
// thread tokenizes with an instance of its own.
unsafe impl Sync for Tokenizers<'_> {}
pub(crate) struct Tokenizer<'t> {
    module: &'t ffi::fts5_tokenizer,
    instance: *mut ffi::Fts5Tokenizer,
}
impl Drop for Tokenizer<'_> {
    fn drop(&mut self) {
        if let Some(delete) = self.module.xDelete {
            unsafe { delete(self.instance) }
        }
    }
}
impl<'c> Tokenizers<'c> {
    pub(crate) fn new(c: &'c Connection) -> Result<Self> {
        let api = crate::fts_span::api(c)?;
        let mut module = ffi::fts5_tokenizer {
            xCreate: None,
            xDelete: None,
            xTokenize: None,
        };
        let mut user = ptr::null_mut();
        let Some(find) = (unsafe { (*api).xFindTokenizer }) else {
            bail!("FTS5 tokenizer lookup is unavailable");
        };
        let rc = unsafe { find(api, c"unicode61".as_ptr(), &mut user, &mut module) };
        ensure!(
            rc == ffi::SQLITE_OK,
            "unicode61 tokenizer is unavailable: {rc}"
        );
        Ok(Self {
            module,
            user,
            _connection: PhantomData,
        })
    }
    pub(crate) fn tokenizer(&self) -> Result<Tokenizer<'_>> {
        let Some(create) = self.module.xCreate else {
            bail!("unicode61 tokenizer cannot be created");
        };
        let mut args = [c"remove_diacritics".as_ptr(), c"0".as_ptr()];
        let mut instance = ptr::null_mut();
        let rc = unsafe { create(self.user, args.as_mut_ptr(), 2, &mut instance) };
        ensure!(
            rc == ffi::SQLITE_OK && !instance.is_null(),
            "unicode61 tokenizer cannot be created: {rc}"
        );
        Ok(Tokenizer {
            module: &self.module,
            instance,
        })
    }
}
impl Tokenizer<'_> {
    /// Every token FTS5 stores for this document text, in order.
    pub(crate) fn tokens(&mut self, text: &str, mut each: impl FnMut(&[u8])) -> Result<()> {
        unsafe extern "C" fn token(
            context: *mut c_void,
            _flags: c_int,
            token: *const c_char,
            length: c_int,
            _start: c_int,
            _end: c_int,
        ) -> c_int {
            let each = &mut *context.cast::<&mut dyn FnMut(&[u8])>();
            // fts5StorageInsertCallback stores at most FTS5_MAX_TOKEN_SIZE bytes.
            let length = length.clamp(0, 32_768) as usize;
            each(std::slice::from_raw_parts(token.cast::<u8>(), length));
            ffi::SQLITE_OK
        }
        let Some(tokenize) = self.module.xTokenize else {
            bail!("unicode61 tokenizer cannot tokenize");
        };
        let length = c_int::try_from(text.len())?;
        let mut each: &mut dyn FnMut(&[u8]) = &mut each;
        let rc = unsafe {
            tokenize(
                self.instance,
                (&mut each as *mut &mut dyn FnMut(&[u8])).cast(),
                ffi::FTS5_TOKENIZE_DOCUMENT,
                text.as_ptr().cast(),
                length,
                Some(token),
            )
        };
        ensure!(rc == ffi::SQLITE_OK, "tool text tokenization failed: {rc}");
        Ok(())
    }
}
/// Dictionary words have at least three characters, like `length(term)>=3`.
fn dictionary_word(token: &[u8]) -> bool {
    token.iter().filter(|&&b| b & 0xc0 != 0x80).nth(2).is_some()
}
/// Sorted distinct block numbers: varint deltas, or a bitmap from the first
/// block when that is shorter.
pub(crate) fn encode_blocks(blocks: &[u32]) -> Vec<u8> {
    let Some((&first, &last)) = blocks.first().zip(blocks.last()) else {
        return Vec::new();
    };
    let mut deltas = vec![0];
    store::put_varint(first, &mut deltas);
    for pair in blocks.windows(2) {
        store::put_varint(pair[1] - pair[0], &mut deltas);
    }
    let span = (last - first) as usize / 8 + 1;
    if span + 6 >= deltas.len() {
        return deltas;
    }
    let mut bitmap = vec![1];
    store::put_varint(first, &mut bitmap);
    let start = bitmap.len();
    bitmap.resize(start + span, 0);
    for &block in blocks {
        let offset = (block - first) as usize;
        bitmap[start + offset / 8] |= 1 << (offset % 8);
    }
    bitmap
}
pub(crate) fn decode_blocks(bytes: &[u8]) -> Result<Vec<u32>> {
    let Some((&tag, body)) = bytes.split_first() else {
        return Ok(Vec::new());
    };
    let mut at = 0;
    let first = store::take_varint(body, &mut at)?;
    let mut out = vec![first];
    match tag {
        0 => {
            while at < body.len() {
                let delta = store::take_varint(body, &mut at)?;
                ensure!(delta > 0, "unsorted tool word blocks");
                let next = out[out.len() - 1]
                    .checked_add(delta)
                    .ok_or_else(|| anyhow::anyhow!("tool word block overflow"))?;
                out.push(next);
            }
        }
        1 => {
            out.clear();
            for (index, &byte) in body[at..].iter().enumerate() {
                for bit in 0..8 {
                    if byte & (1 << bit) != 0 {
                        out.push(
                            u32::try_from(index * 8 + bit)
                                .ok()
                                .and_then(|offset| first.checked_add(offset))
                                .ok_or_else(|| anyhow::anyhow!("tool word block overflow"))?,
                        );
                    }
                }
            }
            ensure!(out.first() == Some(&first), "invalid tool word bitmap");
        }
        _ => bail!("invalid tool word blocks"),
    }
    Ok(out)
}
fn shard(token: &[u8]) -> usize {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for &byte in token {
        hash = (hash ^ u64::from(byte)).wrapping_mul(0x100_0000_01b3);
    }
    (hash >> 32) as usize % SHARDS
}
const SHARDS: usize = 16;
pub(crate) type Presence = Vec<HashMap<Box<[u8]>, Vec<u32>>>;
pub(crate) fn new_presence() -> Presence {
    (0..SHARDS).map(|_| HashMap::new()).collect()
}
/// Distinct dictionary words of one row's indexed text.
pub(crate) fn row_words(tokenizer: &mut Tokenizer<'_>, text: &str) -> Result<Vec<Box<[u8]>>> {
    let mut seen: HashSet<Box<[u8]>> = HashSet::new();
    tokenizer.tokens(text, |token| {
        if dictionary_word(token) && !seen.contains(token) {
            seen.insert(token.into());
        }
    })?;
    Ok(seen.into_iter().collect())
}
/// Records rows' words. Rows must arrive in increasing id order; each shard
/// merges on its own thread.
pub(crate) fn add_rows(presence: &mut Presence, rows: Vec<(i64, Vec<Box<[u8]>>)>) -> Result<()> {
    let mut parts: Vec<Vec<(u32, Box<[u8]>)>> = (0..SHARDS).map(|_| Vec::new()).collect();
    for (id, words) in rows {
        let block = u32::try_from(id / store::BLOCK)?;
        for word in words {
            parts[shard(&word)].push((block, word));
        }
    }
    presence.par_iter_mut().zip(parts).for_each(|(map, part)| {
        for (block, word) in part {
            let blocks = map.entry(word).or_default();
            if blocks.last() != Some(&block) {
                blocks.push(block);
            }
        }
    });
    Ok(())
}
/// One block's distinct words, split by shard.
type ShardedBlock = (u32, Vec<Vec<Box<[u8]>>>);
/// One shard's words of one block.
type BlockPart = (u32, Vec<Box<[u8]>>);
/// Blocks containing each dictionary word, over every visible tool row.
fn presence(tx: &Transaction<'_>) -> Result<Presence> {
    let tokenizers = Tokenizers::new(tx)?;
    let mut shards = new_presence();
    let mut statement = tx.prepare("SELECT id,text FROM tools WHERE visible=1 ORDER BY id")?;
    let mut rows = statement.query([])?;
    let mut carry: Option<(i64, Vec<u8>)> = None;
    loop {
        // Whole blocks only, so each block's distinct words are found once.
        let mut blocks: Vec<(u32, Vec<Vec<u8>>)> = Vec::new();
        let mut count = 0;
        let mut ended = false;
        loop {
            let (id, text) = match carry.take() {
                Some(row) => row,
                None => match rows.next()? {
                    Some(row) => (row.get(0)?, row.get(1)?),
                    None => {
                        ended = true;
                        break;
                    }
                },
            };
            let block = u32::try_from(id / store::BLOCK)?;
            if blocks.last().is_some_and(|(last, _)| *last == block) {
                blocks.last_mut().unwrap().1.push(text);
            } else if count >= 16_384 {
                carry = Some((id, text));
                break;
            } else {
                blocks.push((block, vec![text]));
            }
            count += 1;
        }
        let words: Vec<ShardedBlock> = blocks
            .into_par_iter()
            .map(|(block, texts)| {
                let mut tokenizer = tokenizers.tokenizer()?;
                let mut seen: HashSet<Box<[u8]>> = HashSet::new();
                for bytes in texts {
                    let text = store::normalize(&store::decode("tools", bytes)?);
                    tokenizer.tokens(&index_text(&text), |token| {
                        if dictionary_word(token) && !seen.contains(token) {
                            seen.insert(token.into());
                        }
                    })?;
                }
                let mut parts = vec![Vec::new(); SHARDS];
                for token in seen {
                    parts[shard(&token)].push(token);
                }
                Ok((block, parts))
            })
            .collect::<Result<_>>()?;
        let mut parts: Vec<Vec<BlockPart>> = vec![Vec::new(); SHARDS];
        for (block, shards) in words {
            for (part, tokens) in parts.iter_mut().zip(shards) {
                part.push((block, tokens));
            }
        }
        shards.par_iter_mut().zip(parts).for_each(|(map, part)| {
            for (block, tokens) in part {
                for token in tokens {
                    map.entry(token).or_default().push(block);
                }
            }
        });
        if ended {
            return Ok(shards);
        }
    }
}
/// Distinct lowercase ASCII trigrams of a dictionary word, as base-36 codes.
fn trigrams(term: &[u8]) -> Vec<u32> {
    let mut codes: Vec<u32> = term
        .windows(3)
        .filter_map(|window| {
            window.iter().try_fold(0u32, |code, &b| {
                let digit = match b {
                    b'0'..=b'9' => b - b'0',
                    b'a'..=b'z' => b - b'a' + 10,
                    _ => return None,
                };
                Some(code * 36 + u32::from(digit))
            })
        })
        .collect();
    codes.sort_unstable();
    codes.dedup();
    codes
}
/// The summed frequency of the dictionary words containing this three-character
/// term: exactly what its expansion would total. None when unknown or zero.
pub(crate) fn trigram_doc(c: &Connection, term: &str) -> Result<Option<usize>> {
    let codes = trigrams(term.as_bytes());
    if term.len() != 3 || codes.len() != 1 {
        return Ok(None);
    }
    Ok(c.query_row(
        "SELECT doc FROM tool_trigrams WHERE gram=?",
        [codes[0]],
        |r| r.get::<_, i64>(0),
    )
    .optional()?
    .filter(|&doc| doc > 0)
    .map(|doc| doc as usize))
}
/// `collected` is the first build's presence, gathered while its FTS rows were
/// prepared; a rebuild without it reads every visible row once more.
pub(crate) fn refresh(
    tx: &Transaction<'_>,
    first: bool,
    collected: Option<Presence>,
) -> Result<()> {
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
        let mut shards = match collected {
            Some(shards) => shards,
            None => presence(tx)?,
        };
        tx.execute_batch("INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('automerge',0);INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('crisismerge',64);INSERT INTO tool_lexicon(tool_lexicon,rank) VALUES('hashsize',16777216);")?;
        let mut sums = vec![0i64; 36 * 36 * 36];
        {
            let mut vocabulary =
                tx.prepare("SELECT term,doc FROM tools_vocab WHERE length(term)>=3")?;
            let mut rows = vocabulary.query([])?;
            let mut insert = tx.prepare("INSERT INTO tool_words(term,doc,blocks) VALUES(?1,?2,?3) ON CONFLICT(term) DO UPDATE SET doc=excluded.doc,blocks=excluded.blocks")?;
            while let Some(row) = rows.next()? {
                let term = row.get_ref(0)?.as_bytes()?;
                let blocks = shards[shard(term)].remove(term).unwrap_or_default();
                let doc: i64 = row.get(1)?;
                // Every word the index holds came from a visible row's tokens.
                ensure!(
                    doc == 0 || !blocks.is_empty(),
                    "tool word presence is incomplete"
                );
                for code in trigrams(term) {
                    sums[code as usize] += doc;
                }
                insert.execute(params![
                    row.get_ref(0)?.as_str()?,
                    doc,
                    encode_blocks(&blocks)
                ])?;
            }
        }
        let mut insert = tx.prepare("INSERT INTO tool_trigrams(gram,doc) VALUES(?1,?2) ON CONFLICT(gram) DO UPDATE SET doc=excluded.doc")?;
        for (code, &doc) in sums.iter().enumerate() {
            if doc != 0 {
                insert.execute(params![code as i64, doc])?;
            }
        }
        tx.execute(
            "INSERT INTO tool_lexicon(tool_lexicon) VALUES('optimize')",
            [],
        )?;
        crate::fts_segments::configure(tx, "tool_lexicon")?;
    } else {
        let tokenizers = Tokenizers::new(tx)?;
        let mut tokenizer = tokenizers.tokenizer()?;
        let mut added: HashMap<Box<[u8]>, Vec<u32>> = HashMap::new();
        {
            let mut insert = tx.prepare("INSERT INTO tool_word_batch(text) VALUES(?)")?;
            let mut q = tx.prepare(
                "SELECT p.id,p.text FROM tool_word_dirty d JOIN tools p ON p.id=d.id WHERE p.visible=1",
            )?;
            let mut rows = q.query([])?;
            while let Some(row) = rows.next()? {
                let block = u32::try_from(row.get::<_, i64>(0)? / store::BLOCK)?;
                let text = store::normalize(&store::decode("tools", row.get(1)?)?);
                let text = index_text(&text);
                tokenizer.tokens(&text, |token| {
                    if dictionary_word(token) {
                        match added.get_mut(token) {
                            Some(blocks) => blocks.push(block),
                            None => {
                                added.insert(token.into(), vec![block]);
                            }
                        }
                    }
                })?;
                insert.execute([text.as_ref()])?;
            }
            let mut q = tx.prepare("SELECT text FROM tool_word_old")?;
            let mut rows = q.query([])?;
            while let Some(row) = rows.next()? {
                let text = store::normalize(&store::decode("tools", row.get(0)?)?);
                insert.execute([index_text(&text).as_ref()])?;
            }
        }
        {
            // Frequencies come from the index itself; their deltas keep each
            // trigram's sum equal to the total its expansion would compute.
            let mut deltas: HashMap<u32, i64> = HashMap::new();
            let mut vocabulary = tx.prepare("SELECT b.term,coalesce((SELECT doc FROM tools_vocab WHERE term=b.term),0) FROM tool_word_batch_vocab b WHERE length(b.term)>=3")?;
            let mut select = tx.prepare("SELECT id,doc FROM tool_words WHERE term=?")?;
            let mut update = tx.prepare("UPDATE tool_words SET doc=?2 WHERE id=?1")?;
            let mut new_words = Vec::new();
            let mut rows = vocabulary.query([])?;
            while let Some(row) = rows.next()? {
                let term: String = row.get(0)?;
                let doc: i64 = row.get(1)?;
                let old = select
                    .query_row([&term], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))
                    .optional()?;
                let delta = match old {
                    None => doc,
                    Some((id, before)) => {
                        if before != doc {
                            update.execute(params![id, doc])?;
                        }
                        doc - before
                    }
                };
                if delta != 0 {
                    for code in trigrams(term.as_bytes()) {
                        *deltas.entry(code).or_default() += delta;
                    }
                }
                if old.is_none() {
                    new_words.push((term, doc));
                }
            }
            drop(rows);
            // The batch is discarded before any statement opens a savepoint, so its
            // pending terms never become a segment.
            tx.execute(
                "INSERT INTO tool_word_batch(tool_word_batch) VALUES('delete-all')",
                [],
            )?;
            // One statement: the lexicon rows its trigger writes join a single segment.
            tx.execute(
                "INSERT INTO tool_words(term,doc) SELECT value->>0,value->>1 FROM json_each(?1)",
                [serde_json::to_string(&new_words)?],
            )?;
            let mut apply = tx.prepare("INSERT INTO tool_trigrams(gram,doc) VALUES(?1,?2) ON CONFLICT(gram) DO UPDATE SET doc=doc+excluded.doc")?;
            for (code, delta) in deltas {
                if delta != 0 {
                    apply.execute(params![code, delta])?;
                }
            }
        }
        // Removed rows leave their blocks listed: presence may only overstate.
        let mut select = tx.prepare_cached("SELECT id,blocks FROM tool_words WHERE term=?")?;
        let mut update = tx.prepare_cached("UPDATE tool_words SET blocks=?2 WHERE id=?1")?;
        for (token, mut blocks) in added {
            let term = std::str::from_utf8(&token)?;
            let Some((id, old)) = select
                .query_row([term], |r| {
                    Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?))
                })
                .optional()?
            else {
                bail!("tool word presence is incomplete");
            };
            let mut merged = decode_blocks(&old)?;
            let before = merged.len();
            merged.append(&mut blocks);
            merged.sort_unstable();
            merged.dedup();
            if merged.len() != before {
                update.execute(params![id, encode_blocks(&merged)])?;
            }
        }
    }
    tx.execute("DELETE FROM tool_word_dirty", [])?;
    tx.execute("DELETE FROM tool_word_old", [])?;
    Ok(())
}
pub(crate) struct Word {
    pub term: String,
    pub doc: usize,
    pub blocks: Vec<u32>,
}
/// None means the literal may span tokenizer boundaries and must use verified block scanning.
pub(crate) fn expand(c: &Connection, term: &str) -> Result<Option<Vec<Word>>> {
    if term.len() < 3 || term.len() > 4096 || !term.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return Ok(None);
    }
    let expression = term
        .as_bytes()
        .windows(3)
        .map(|g| format!("\"{}\"", std::str::from_utf8(g).unwrap()))
        .collect::<Vec<_>>()
        .join(" AND ");
    let mut q=c.prepare("SELECT w.term,w.doc,w.blocks FROM tool_lexicon l CROSS JOIN tool_words w ON w.id=l.rowid WHERE tool_lexicon MATCH ?1 AND instr(w.term,?2)>0 AND w.doc>0")?;
    let mut rows = q.query([&expression, term])?;
    let mut words = Vec::new();
    while let Some(row) = rows.next()? {
        words.push(Word {
            term: row.get(0)?,
            doc: row.get(1)?,
            blocks: decode_blocks(row.get_ref(2)?.as_blob()?)?,
        });
    }
    Ok(Some(words))
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
mod fixture {
    use super::*;
    pub(super) fn open() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        c.execute("INSERT INTO event_sessions VALUES(1,'pi','fixture')", [])
            .unwrap();
        c
    }
    pub(super) fn insert(tx: &Transaction<'_>, id: i64, text: &str, visible: bool) {
        tx.execute(
            "INSERT INTO tool_blobs(id,digest,body) VALUES(?1,CAST(?1 AS BLOB),?2)",
            params![id, store::encode_tool(text).unwrap()],
        )
        .unwrap();
        tx.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key,blob,visible) VALUES(?1,?1,1,1,?1,0,0,0,0,0,0,0,0,'','','',0,?1,?2)", params![id, visible]).unwrap();
        if visible {
            tx.execute(
                "INSERT INTO tools_fts(rowid,text) VALUES(?1,?2)",
                params![id, index_text(&store::normalize(text)).as_ref()],
            )
            .unwrap();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dictionary_keeps_substrings_past_sqlite_token_limit() {
        let mut c = fixture::open();
        let text = format!("{}needle{}", "x".repeat(40_000), "y".repeat(40_000));
        let tx = c.transaction().unwrap();
        fixture::insert(&tx, 1, &text, true);
        refresh(&tx, true, None).unwrap();
        tx.commit().unwrap();
        let words = expand(&c, "needle").unwrap().unwrap();
        assert!(!words.is_empty());
        let expression = words
            .iter()
            .map(|word| format!("\"{}\"", word.term))
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
        let mut c = fixture::open();
        for (id, first) in [(1, true), (2, false)] {
            let tx = c.transaction().unwrap();
            fixture::insert(&tx, id, "needle", true);
            refresh(&tx, first, None).unwrap();
            tx.commit().unwrap();
            assert_eq!(
                expand(&c, "needle")
                    .unwrap()
                    .unwrap()
                    .into_iter()
                    .map(|w| (w.term, w.doc, w.blocks))
                    .collect::<Vec<_>>(),
                vec![("needle".into(), 1, vec![0])]
            );
            let tx = c.transaction().unwrap();
            tx.execute("DELETE FROM tools_fts WHERE rowid=?", [id])
                .unwrap();
            tx.execute("DELETE FROM tools WHERE id=?", [id]).unwrap();
            refresh(&tx, false, None).unwrap();
            tx.commit().unwrap();
            assert!(expand(&c, "needle").unwrap().unwrap().is_empty());
        }
    }
}

#[cfg(test)]
mod presence_tests {
    use super::*;
    use std::collections::BTreeSet;
    /// Exact blocks of every dictionary word, taken from the FTS index itself.
    fn truth(c: &Connection) -> Vec<(String, BTreeSet<u32>)> {
        let words: Vec<String> = c
            .prepare("SELECT term FROM tool_words WHERE doc>0 ORDER BY term")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        words
            .into_iter()
            .map(|word| {
                let blocks = c
                    .prepare("SELECT rowid FROM tools_fts WHERE tools_fts MATCH ?")
                    .unwrap()
                    .query_map([format!("\"{}\"", word.replace('"', "\"\""))], |r| {
                        r.get::<_, i64>(0)
                    })
                    .unwrap()
                    .map(|id| (id.unwrap() / store::BLOCK) as u32)
                    .collect();
                (word, blocks)
            })
            .collect()
    }
    fn stored(c: &Connection, word: &str) -> BTreeSet<u32> {
        decode_blocks(
            &c.query_row("SELECT blocks FROM tool_words WHERE term=?", [word], |r| {
                r.get::<_, Vec<u8>>(0)
            })
            .unwrap(),
        )
        .unwrap()
        .into_iter()
        .collect()
    }
    #[test]
    fn block_lists_round_trip_both_encodings() {
        let dense: Vec<u32> = (40..2000).filter(|n| n % 3 != 0).collect();
        let sparse = vec![7, 9, 70_000, 4_000_000_000];
        for blocks in [vec![], vec![0], vec![5], dense, sparse] {
            let encoded = encode_blocks(&blocks);
            assert_eq!(decode_blocks(&encoded).unwrap(), blocks);
        }
        assert_eq!(encode_blocks(&(0..64).collect::<Vec<_>>())[0], 1);
        assert_eq!(encode_blocks(&[1, 900])[0], 0);
        assert!(decode_blocks(&[2, 1]).is_err());
    }
    #[test]
    fn presence_equals_indexed_tokens_and_only_grows_after_changes() {
        // Read in a separate pass, and collected while FTS rows are prepared.
        for collect in [false, true] {
            check_presence(collect);
        }
    }
    fn check_presence(collect: bool) {
        let mut c = fixture::open();
        // Unicode letters, combining marks, private-use and number forms all
        // take unicode61's own token rules; the windowed long word crosses blocks.
        let texts = [
            "Release workflow: prerelease_workflow2 reléase",
            "naïve café ＦＵＬＬ ｗｉｄｔｈ e\u{301}tude \u{e000}glyph x²y ⑦seven",
            "",
            "cargo build --release && ./target/release/agrep",
        ];
        let long = format!("{}needle{}", "q".repeat(9_000), "z".repeat(9_000));
        let tx = c.transaction().unwrap();
        for id in 0..1600i64 {
            let text = if id == 1030 {
                long.clone()
            } else {
                format!("{} row{}", texts[(id % 4) as usize], id % 37)
            };
            fixture::insert(&tx, id, &text, id % 11 != 5);
        }
        let collected = if collect {
            let tokenizers = Tokenizers::new(&tx).unwrap();
            let mut tokenizer = tokenizers.tokenizer().unwrap();
            let mut presence = new_presence();
            let mut q = tx
                .prepare("SELECT id,text FROM tools WHERE visible=1 ORDER BY id")
                .unwrap();
            let mut rows = q.query([]).unwrap();
            while let Some(row) = rows.next().unwrap() {
                let text = store::normalize(&store::decode("tools", row.get(1).unwrap()).unwrap());
                let words = row_words(&mut tokenizer, &index_text(&text)).unwrap();
                add_rows(&mut presence, vec![(row.get(0).unwrap(), words)]).unwrap();
            }
            Some(presence)
        } else {
            None
        };
        refresh(&tx, true, collected).unwrap();
        tx.commit().unwrap();
        let first = truth(&c);
        assert!(first.len() > 20);
        let sums = |c: &Connection| {
            for term in ["rel", "ase", "flo", "row", "ow1", "zzz", "caf"] {
                let expanded: usize = expand(c, term)
                    .unwrap()
                    .unwrap()
                    .iter()
                    .map(|w| w.doc)
                    .sum();
                assert_eq!(
                    trigram_doc(c, term).unwrap().unwrap_or(0),
                    expanded,
                    "{term}"
                );
            }
        };
        for (word, blocks) in &first {
            assert_eq!(&stored(&c, word), blocks, "{word}");
        }
        sums(&c);
        let tx = c.transaction().unwrap();
        fixture::insert(&tx, 2100, "brandnewword release", true);
        tx.execute("DELETE FROM tools_fts WHERE rowid=4", [])
            .unwrap();
        tx.execute("DELETE FROM tools WHERE id=4", []).unwrap();
        refresh(&tx, false, None).unwrap();
        tx.commit().unwrap();
        for (word, blocks) in truth(&c) {
            assert!(stored(&c, &word).is_superset(&blocks), "{word}");
        }
        assert_eq!(stored(&c, "brandnewword"), BTreeSet::from([4]));
        assert!(stored(&c, "release").contains(&4));
        let expanded = expand(&c, "elease").unwrap().unwrap();
        assert!(expanded.iter().any(|w| w.term == "prerelease"));
        for word in expanded {
            assert_eq!(
                word.blocks,
                stored(&c, &word.term).into_iter().collect::<Vec<_>>()
            );
        }
        sums(&c);
    }
}
