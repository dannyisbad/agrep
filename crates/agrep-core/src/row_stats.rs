//! Exact family statistics updated from changed prose contributions.
use crate::boundary_stats::TokenSink;
use anyhow::{Context, Result};
use rayon::prelude::*;
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde::{Serialize, Serializer};
use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::Mutex;

pub(crate) fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS row_stats(id INTEGER PRIMARY KEY,agent TEXT NOT NULL,family TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS family_token_counts(agent TEXT NOT NULL,family TEXT NOT NULL,start TEXT NOT NULL,body BLOB NOT NULL,PRIMARY KEY(agent,family,start)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stats_dirty(id INTEGER PRIMARY KEY,old_text BLOB,old_agent TEXT,old_family TEXT);
CREATE TRIGGER IF NOT EXISTS prose_stats_insert AFTER INSERT ON prose BEGIN INSERT OR IGNORE INTO stats_dirty(id) VALUES(new.id); END;
CREATE TRIGGER IF NOT EXISTS prose_stats_delete BEFORE DELETE ON prose BEGIN INSERT OR IGNORE INTO stats_dirty SELECT old.id,CASE WHEN r.id IS NOT NULL THEN old.text END,r.agent,r.family FROM (SELECT 1) LEFT JOIN row_stats r ON r.id=old.id; END;
CREATE TRIGGER IF NOT EXISTS prose_stats_update BEFORE UPDATE OF text,visible,agent,session ON prose BEGIN INSERT OR IGNORE INTO stats_dirty SELECT old.id,CASE WHEN r.id IS NOT NULL THEN old.text END,r.agent,r.family FROM (SELECT 1) LEFT JOIN row_stats r ON r.id=old.id; END;
CREATE TRIGGER IF NOT EXISTS session_stats_family BEFORE UPDATE OF family ON sessions WHEN old.family!=new.family BEGIN INSERT OR IGNORE INTO stats_dirty SELECT p.id,CASE WHEN r.id IS NOT NULL THEN p.text END,r.agent,r.family FROM prose p LEFT JOIN row_stats r ON r.id=p.id WHERE p.agent=old.agent AND p.session=old.id; END;")?;
    Ok(())
}
const FAMILY_PAGE: usize = 256;
#[derive(Default)]
struct Family {
    key: Option<(String, String)>,
    counts: HashMap<String, [i64; 2]>,
    delta: HashMap<String, [i64; 2]>,
}
fn write_family_page(
    tx: &Transaction<'_>,
    agent: &str,
    family: &str,
    start: &str,
    counts: &HashMap<String, [u32; 2]>,
) -> Result<()> {
    let body = lz4_flex::compress_prepend_size(&serde_json::to_vec(counts)?);
    tx.prepare_cached("INSERT INTO family_token_counts VALUES(?1,?2,?3,?4) ON CONFLICT(agent,family,start) DO UPDATE SET body=excluded.body")?.execute(params![agent,family,start,body])?;
    Ok(())
}
impl Family {
    fn flush(&mut self, tx: &Transaction<'_>) -> Result<()> {
        let Some((agent, family)) = &self.key else {
            return Ok(());
        };
        let mut pages: HashMap<String, HashMap<String, [u32; 2]>> = HashMap::new();
        let mut locate = tx.prepare_cached("SELECT start FROM family_token_counts WHERE agent=?1 AND family=?2 AND start<=?3 ORDER BY start DESC LIMIT 1")?;
        for (token, change) in std::mem::take(&mut self.counts) {
            if change == [0, 0] {
                continue;
            }
            let page: Option<String> = locate
                .query_row(params![agent, family, token], |r| r.get(0))
                .optional()?;
            let start = page.as_deref().unwrap_or("").to_owned();
            if !pages.contains_key(&start) {
                let counts = if page.is_some() {
                    let body: Vec<u8> = tx.prepare_cached("SELECT body FROM family_token_counts WHERE agent=?1 AND family=?2 AND start=?3")?.query_row(params![agent,family,start], |r| r.get(0))?;
                    serde_json::from_slice(&lz4_flex::decompress_size_prepended(&body)?)?
                } else {
                    HashMap::new()
                };
                pages.insert(start.clone(), counts);
            }
            let counts = pages.get_mut(&start).expect("loaded family page");
            let n = counts.entry(token.clone()).or_default();
            for column in 0..2 {
                let before = n[column] > 0;
                n[column] = u32::try_from(i64::from(n[column]) + change[column])
                    .context("family token count overflow or underflow")?;
                self.delta.entry(token.clone()).or_default()[column] +=
                    i64::from(n[column] > 0) - i64::from(before);
            }
            if *n == [0, 0] {
                counts.remove(&token);
            }
        }
        for (start, counts) in pages {
            if counts.len() <= FAMILY_PAGE {
                write_family_page(tx, agent, family, &start, &counts)?;
            } else {
                let mut items: Vec<_> = counts.into_iter().collect();
                items.sort_unstable_by(|a, b| a.0.cmp(&b.0));
                for (index, chunk) in items.chunks(FAMILY_PAGE).enumerate() {
                    let lower = if index == 0 {
                        start.as_str()
                    } else {
                        &chunk[0].0
                    };
                    write_family_page(tx, agent, family, lower, &chunk.iter().cloned().collect())?;
                }
            }
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
        self.apply_tokens(
            tx,
            agent,
            family,
            crate::boundary_stats::text_tokens(std::iter::once(text)),
            sign,
        )
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
            self.key = Some((agent, family));
        }
        for (column, tokens) in [(0, any), (1, aligned)] {
            for token in tokens {
                self.counts.entry(token).or_default()[column] += sign;
            }
        }
        Ok(())
    }
}
/// Mixes a packed token for hashbrown, which picks buckets by the low bits.
#[derive(Default)]
struct PackedHasher(u64);
impl Hasher for PackedHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for &byte in bytes {
            self.write_u32(u32::from(byte));
        }
    }
    fn write_u32(&mut self, n: u32) {
        let product = u128::from(self.0 ^ u64::from(n)) * 0x9e37_79b9_7f4a_7c15;
        self.0 = product as u64 ^ (product >> 64) as u64;
    }
}
type Packed<V> = HashMap<u32, V, BuildHasherDefault<PackedHasher>>;
/// Nearly every token has at most four bytes and is keyed without allocation.
/// Zero padding is unambiguous because a packed token never contains NUL.
fn pack(token: &str) -> Option<u32> {
    let bytes = token.as_bytes();
    (bytes.len() <= 4 && !bytes.contains(&0)).then(|| {
        let mut key = [0; 4];
        key[..bytes.len()].copy_from_slice(bytes);
        u32::from_le_bytes(key)
    })
}
fn unpack(key: u32, buffer: &mut [u8; 4]) -> &str {
    *buffer = key.to_le_bytes();
    let len = buffer.iter().position(|byte| *byte == 0).unwrap_or(4);
    std::str::from_utf8(&buffer[..len]).expect("a packed token is UTF-8")
}
#[derive(Clone, Copy, Default)]
struct Tally {
    rows: [u32; 2],
    last: [u32; 2],
}
/// One family's rows holding each token anywhere / aligned, each row counted once.
/// Serialized, it is the `family_token_counts` body.
#[derive(Default)]
struct Rows {
    row: u32,
    short: Packed<Tally>,
    long: HashMap<Box<str>, Tally>,
}
impl Rows {
    fn tally(&mut self, batch: &Batch) -> Result<()> {
        self.row = 0;
        self.short.clear();
        self.long.clear();
        let mut start = 0;
        for &end in &batch.ends {
            self.row = self.row.checked_add(1).context("family row overflow")?;
            crate::boundary_stats::row_tokens(&batch.text[start..end], self);
            start = end;
        }
        Ok(())
    }
}
impl TokenSink for Rows {
    fn token(&mut self, aligned: bool, token: &str) {
        let tally = match pack(token) {
            Some(key) => self.short.entry(key).or_default(),
            None => {
                if !self.long.contains_key(token) {
                    self.long.insert(token.into(), Tally::default());
                }
                self.long.get_mut(token).expect("inserted above")
            }
        };
        let column = usize::from(aligned);
        if tally.last[column] != self.row {
            tally.last[column] = self.row;
            tally.rows[column] += 1;
        }
    }
}
enum Key<'a> {
    Short(u32),
    Long(&'a str),
}
impl Serialize for Key<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        match self {
            Key::Short(key) => serializer.serialize_str(unpack(*key, &mut [0; 4])),
            Key::Long(token) => serializer.serialize_str(token),
        }
    }
}
impl Serialize for Rows {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        serializer.collect_map(
            self.short
                .iter()
                .map(|(key, tally)| (Key::Short(*key), tally.rows))
                .chain(
                    self.long
                        .iter()
                        .map(|(token, tally)| (Key::Long(token), tally.rows)),
                ),
        )
    }
}
/// Families holding each token anywhere / aligned: `term_stats` n and s.
#[derive(Default)]
struct Families {
    short: Packed<[u32; 2]>,
    long: HashMap<Box<str>, [u32; 2]>,
}
impl Families {
    fn count(&mut self, rows: &Rows) {
        for (key, tally) in &rows.short {
            let n = self.short.entry(*key).or_default();
            n[0] += u32::from(tally.rows[0] > 0);
            n[1] += u32::from(tally.rows[1] > 0);
        }
        for (token, tally) in &rows.long {
            if !self.long.contains_key(token) {
                self.long.insert(token.clone(), [0; 2]);
            }
            let n = self.long.get_mut(token).expect("inserted above");
            n[0] += u32::from(tally.rows[0] > 0);
            n[1] += u32::from(tally.rows[1] > 0);
        }
    }
    fn merge(&mut self, other: Families) {
        for (key, [n, s]) in other.short {
            let total = self.short.entry(key).or_default();
            total[0] += n;
            total[1] += s;
        }
        for (token, [n, s]) in other.long {
            let total = self.long.entry(token).or_default();
            total[0] += n;
            total[1] += s;
        }
    }
}
/// One family's visible rows, concatenated; `ends` holds each row's end offset.
struct Batch {
    family: u32,
    text: String,
    ends: Vec<usize>,
}
const NO_FAMILY: u32 = u32::MAX;
/// Initial construction has no old contribution. Rows are read in id order, then
/// each family's rows in id runs, and families are tallied in parallel.
/// The ordinary refresh below retains its exact changed-row accounting.
pub(crate) fn build(tx: &Transaction<'_>) -> Result<()> {
    build_in_chunks(tx, 16_384)
}
/// `chunk_rows` rows or more go to the tally workers at a time.
fn build_in_chunks(tx: &Transaction<'_>, chunk_rows: usize) -> Result<()> {
    let mut families: Vec<(String, String)> = Vec::new();
    let mut members: HashMap<String, HashMap<String, u32>> = HashMap::new();
    {
        let mut q = tx.prepare("SELECT agent,family,id FROM sessions ORDER BY agent,family")?;
        let mut rows = q.query([])?;
        while let Some(row) = rows.next()? {
            let (agent, family): (String, String) = (row.get(0)?, row.get(1)?);
            if families
                .last()
                .is_none_or(|last| last.0 != agent || last.1 != family)
            {
                families.push((agent.clone(), family));
            }
            let index = u32::try_from(families.len() - 1)?;
            members.entry(agent).or_default().insert(row.get(2)?, index);
        }
    }
    let mut ids = Vec::new();
    let mut owners = Vec::new();
    {
        let mut q = tx.prepare("SELECT id,agent,session FROM prose WHERE visible=1 ORDER BY id")?;
        let mut rows = q.query([])?;
        let mut last: Option<(String, String, u32)> = None;
        while let Some(row) = rows.next()? {
            let agent = row.get_ref(1)?.as_str()?;
            let session = row.get_ref(2)?.as_str()?;
            let owner = match &last {
                Some((a, s, owner)) if a == agent && s == session => *owner,
                _ => {
                    let owner = members
                        .get(agent)
                        .and_then(|sessions| sessions.get(session))
                        .copied()
                        .unwrap_or(NO_FAMILY);
                    last = Some((agent.to_owned(), session.to_owned(), owner));
                    owner
                }
            };
            ids.push(row.get::<_, i64>(0)?);
            owners.push(owner);
        }
    }
    let mut bounds = vec![0usize; families.len() + 1];
    for &owner in &owners {
        if owner != NO_FAMILY {
            bounds[owner as usize + 1] += 1;
        }
    }
    for family in 0..families.len() {
        bounds[family + 1] += bounds[family];
    }
    let mut next = bounds.clone();
    let mut order = vec![0usize; bounds[families.len()]];
    for (at, &owner) in owners.iter().enumerate() {
        if owner != NO_FAMILY {
            order[next[owner as usize]] = at;
            next[owner as usize] += 1;
        }
    }
    let slots: Vec<Mutex<Families>> = (0..rayon::current_num_threads())
        .map(|_| Mutex::default())
        .collect();
    std::thread::scope(|scope| -> Result<()> {
        let (send, receive) = std::sync::mpsc::sync_channel::<Vec<Batch>>(1);
        let (output, bodies) = std::sync::mpsc::channel::<Result<Vec<(u32, Vec<u8>)>>>();
        let slots = &slots;
        let worker = scope.spawn(move || {
            while let Ok(chunk) = receive.recv() {
                let done = chunk
                    .into_par_iter()
                    .map_init(Rows::default, |rows, batch| -> Result<(u32, Vec<u8>)> {
                        rows.tally(&batch)?;
                        let body = lz4_flex::compress_prepend_size(&serde_json::to_vec(&*rows)?);
                        slots[rayon::current_thread_index().unwrap_or(0) % slots.len()]
                            .lock()
                            .expect("family term slot")
                            .count(rows);
                        Ok((batch.family, body))
                    })
                    .collect::<Result<Vec<_>>>();
                let failed = done.is_err();
                if output.send(done).is_err() || failed {
                    break;
                }
            }
        });
        let store = |done: Result<Vec<(u32, Vec<u8>)>>| -> Result<()> {
            for (family, body) in done? {
                let (agent, name) = &families[family as usize];
                let counts: HashMap<String, [u32; 2]> =
                    serde_json::from_slice(&lz4_flex::decompress_size_prepended(&body)?)?;
                let mut items: Vec<_> = counts.into_iter().collect();
                items.sort_unstable_by(|a, b| a.0.cmp(&b.0));
                for (index, chunk) in items.chunks(FAMILY_PAGE).enumerate() {
                    let lower = if index == 0 { "" } else { &chunk[0].0 };
                    write_family_page(tx, agent, name, lower, &chunk.iter().cloned().collect())?;
                }
            }
            Ok(())
        };
        let mut fetch = tx.prepare(
            "SELECT id,text FROM prose WHERE id BETWEEN ?1 AND ?2 AND visible=1 ORDER BY id",
        )?;
        let mut chunk = Vec::new();
        let mut queued = 0;
        for family in 0..families.len() {
            let rows = &order[bounds[family]..bounds[family + 1]];
            if rows.is_empty() {
                continue;
            }
            let mut batch = Batch {
                family: u32::try_from(family)?,
                text: String::new(),
                ends: Vec::with_capacity(rows.len()),
            };
            // A run of consecutive visible rows is exactly the visible rows of its id range.
            for run in rows.chunk_by(|a, b| *b == *a + 1) {
                let (first, last) = (run[0], run[run.len() - 1]);
                let mut found = fetch.query(params![ids[first], ids[last]])?;
                let mut at = first;
                while let Some(row) = found.next()? {
                    anyhow::ensure!(
                        at <= last && row.get::<_, i64>(0)? == ids[at],
                        "visible prose changed during family statistics"
                    );
                    batch
                        .text
                        .push_str(std::str::from_utf8(row.get_ref(1)?.as_blob()?)?);
                    batch.ends.push(batch.text.len());
                    at += 1;
                }
                anyhow::ensure!(
                    at == last + 1,
                    "visible prose changed during family statistics"
                );
            }
            queued += rows.len();
            chunk.push(batch);
            if queued >= chunk_rows {
                queued = 0;
                if send.send(std::mem::take(&mut chunk)).is_err() {
                    break;
                }
                while let Ok(done) = bodies.try_recv() {
                    store(done)?;
                }
            }
        }
        if !chunk.is_empty() {
            let _ = send.send(chunk);
        }
        drop(send);
        tx.execute("INSERT INTO row_stats SELECT p.id,p.agent,s.family FROM prose p CROSS JOIN sessions s ON s.agent=p.agent AND s.id=p.session WHERE p.visible=1", [])?;
        for done in bodies {
            store(done)?;
        }
        worker
            .join()
            .map_err(|_| anyhow::anyhow!("family statistics panicked"))
    })?;
    let mut total = Families::default();
    for slot in slots {
        total.merge(slot.into_inner().expect("family term slot"));
    }
    let mut terms: Vec<(String, [u32; 2])> = total
        .short
        .into_iter()
        .map(|(key, n)| (unpack(key, &mut [0; 4]).to_owned(), n))
        .chain(
            total
                .long
                .into_iter()
                .map(|(token, n)| (token.into_string(), n)),
        )
        .collect();
    terms.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    let mut insert = tx.prepare_cached("INSERT INTO term_stats VALUES(?1,?2,?3)")?;
    for (token, [n, s]) in terms {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    type Bodies = BTreeMap<(String, String), HashMap<String, [u32; 2]>>;
    type Terms = BTreeMap<String, [i64; 2]>;
    type Owners = Vec<(i64, String, String)>;

    const TEXTS: &[&str] = &[
        "ticket 00012345 deepold ordinary historical explanation",
        "deepold's possessive, xdeepoldx interior; DEEPOLD shouted",
        "parseHTTPResponse Base64Encoder v2beta3 HTTPServer iOS17",
        "don't can't it's rock'n'roll 'quoted' trailing' o'clock",
        "café cafe\u{301}'s naïve façade Ångström",
        "日本語のテキスト 中文字 한국어 混合Mixed",
        "\u{fb01}le ＡＢＣ ｄｅｆ \u{2474} ½ \u{2121} ㎏",
        "emoji 👍🏽 family 👨‍👩‍👧 flags 🇺🇸 keycap 1\u{20e3}",
        "nul\u{0}byte and\ttabs\nnewlines\r\nend",
        "Ελληνικά Кириллица עברית العربية देवनागरी",
        "x",
        "",
        "!!! ??? ... --- ___",
        "mixedΑlphaβeta Script东West snake_case kebab-case",
    ];

    fn database() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(
            "CREATE TABLE prose(id INTEGER PRIMARY KEY,agent TEXT NOT NULL,session TEXT NOT NULL,text BLOB NOT NULL,visible INTEGER NOT NULL);
CREATE TABLE sessions(agent TEXT NOT NULL,id TEXT NOT NULL,family TEXT NOT NULL,PRIMARY KEY(agent,id)) WITHOUT ROWID;
CREATE TABLE term_stats(token TEXT PRIMARY KEY,n INTEGER NOT NULL,s INTEGER NOT NULL) WITHOUT ROWID;",
        )
        .unwrap();
        schema(&c).unwrap();
        c
    }

    fn text(i: usize) -> String {
        let mut text = format!(
            "{} {i:06} {}",
            TEXTS[i % TEXTS.len()],
            TEXTS[i * 7 % TEXTS.len()]
        );
        if i.is_multiple_of(997) {
            text.push_str(&"long ".repeat(crate::ingest::REPLY_CAP));
        }
        text
    }

    /// The per-row token sets of the original construction, counted per family.
    fn expected(c: &Connection) -> (Bodies, Terms, Owners) {
        let mut bodies = Bodies::new();
        let mut owners = Owners::new();
        let mut q = c.prepare("SELECT p.id,p.agent,s.family,p.text FROM prose p CROSS JOIN sessions s ON s.agent=p.agent AND s.id=p.session WHERE p.visible=1 ORDER BY p.id").unwrap();
        let mut rows = q.query([]).unwrap();
        while let Some(row) = rows.next().unwrap() {
            let (id, agent, family): (i64, String, String) = (
                row.get(0).unwrap(),
                row.get(1).unwrap(),
                row.get(2).unwrap(),
            );
            let text = String::from_utf8(row.get(3).unwrap()).unwrap();
            let (any, aligned) = crate::boundary_stats::text_tokens(std::iter::once(text.as_str()));
            let counts = bodies.entry((agent.clone(), family.clone())).or_default();
            for (column, tokens) in [(0, any), (1, aligned)] {
                for token in tokens {
                    counts.entry(token).or_default()[column] += 1;
                }
            }
            owners.push((id, agent, family));
        }
        bodies.retain(|_, counts| !counts.is_empty());
        let mut terms = Terms::new();
        for counts in bodies.values() {
            for (token, n) in counts {
                let total = terms.entry(token.clone()).or_default();
                total[0] += i64::from(n[0] > 0);
                total[1] += i64::from(n[1] > 0);
            }
        }
        (bodies, terms, owners)
    }

    fn stored(c: &Connection) -> (Bodies, Terms, Owners) {
        let mut bodies = Bodies::new();
        let mut q = c
            .prepare("SELECT agent,family,body FROM family_token_counts")
            .unwrap();
        let mut rows = q.query([]).unwrap();
        while let Some(row) = rows.next().unwrap() {
            let body: Vec<u8> = row.get(2).unwrap();
            let counts: HashMap<String, [u32; 2]> =
                serde_json::from_slice(&lz4_flex::decompress_size_prepended(&body).unwrap())
                    .unwrap();
            if !counts.is_empty() {
                bodies
                    .entry((row.get(0).unwrap(), row.get(1).unwrap()))
                    .or_default()
                    .extend(counts);
            }
        }
        let terms = c
            .prepare("SELECT token,n,s FROM term_stats")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, [r.get(1)?, r.get(2)?])))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let owners = c
            .prepare("SELECT id,agent,family FROM row_stats ORDER BY id")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        (bodies, terms, owners)
    }

    #[test]
    fn bulk_statistics_equal_per_row_token_sets_and_refresh_from_them() {
        let mut c = database();
        {
            let tx = c.transaction().unwrap();
            for (agent, id, family) in [
                ("pi", "s0", "f0"),
                ("pi", "s1", "f1"),
                ("pi", "s2", "f0"),
                ("pi", "s3", "f2"),
                ("pi", "s4", "f2"),
                ("pi", "s5", "s5"),
                ("codex", "s0", "f0"),
                ("codex", "s6", "f6"),
            ] {
                tx.execute(
                    "INSERT INTO sessions VALUES(?1,?2,?3)",
                    params![agent, id, family],
                )
                .unwrap();
            }
            // Interleaved rows give single-row runs, blocks give long ones; orphan
            // sessions and invisible rows sit inside both.
            let sessions = [
                ("pi", "s0"),
                ("pi", "s1"),
                ("pi", "s2"),
                ("codex", "s0"),
                ("pi", "orphan"),
                ("pi", "s3"),
                ("pi", "s4"),
                ("codex", "s6"),
                ("pi", "s5"),
            ];
            for i in 0..9_000 {
                let (agent, session) = if i < 4_500 {
                    sessions[i % sessions.len()]
                } else {
                    sessions[(i / 500) % sessions.len()]
                };
                tx.execute(
                    "INSERT INTO prose(agent,session,text,visible) VALUES(?1,?2,?3,?4)",
                    params![agent, session, text(i).into_bytes(), i % 13 != 5],
                )
                .unwrap();
            }
            tx.execute("DELETE FROM stats_dirty", []).unwrap();
            build_in_chunks(&tx, 700).unwrap();
            let built = stored(&tx);
            assert_eq!(built, expected(&tx));
            assert!(built
                .0
                .values()
                .any(|counts| counts.keys().any(|t| t.len() > 4)));
            tx.commit().unwrap();
        }
        let tx = c.transaction().unwrap();
        tx.execute("DELETE FROM prose WHERE id % 17 = 3", [])
            .unwrap();
        tx.execute(
            "UPDATE prose SET text=CAST(text || ' amended Δelta' AS BLOB) WHERE id % 19 = 4",
            [],
        )
        .unwrap();
        tx.execute("UPDATE prose SET visible=1-visible WHERE id % 23 = 6", [])
            .unwrap();
        tx.execute(
            "UPDATE sessions SET family='f1' WHERE agent='pi' AND id='s4'",
            [],
        )
        .unwrap();
        for i in 0..300 {
            tx.execute(
                "INSERT INTO prose(agent,session,text,visible) VALUES('pi','s5',?1,1)",
                [text(i * 31 + 5).into_bytes()],
            )
            .unwrap();
        }
        refresh(&tx).unwrap();
        assert_eq!(stored(&tx), expected(&tx));
    }
}
