//! Immutable q8 blocks are fsynced before one SQLite publication transaction.
use crate::{
    data::DataRoot,
    semantic_model::{self, DIM},
    store,
};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Serialize, Serializer};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
};

#[derive(Default, Debug, Clone)]
pub struct Coverage {
    pub embedded: usize,
    pub total: usize,
}
impl Serialize for Coverage {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        // 0.4.9 rounded the fraction to six places (py/semantic.py:341).
        let fraction = if self.total == 0 {
            1.0
        } else {
            crate::window::round6(self.embedded as f64 / self.total as f64)
        };
        serde_json::json!({"indexed":self.embedded,"total":self.total,"pending":self.total.saturating_sub(self.embedded),"fraction":fraction,"complete":!self.partial(),"order":"newest-first"}).serialize(serializer)
    }
}
impl Coverage {
    pub fn partial(&self) -> bool {
        self.embedded < self.total
    }
    /// 0.4.9's `semantic_coverage_notice(.., suppress_trivial=True)` firing
    /// (py/surface_policy.py:2835-2872): partial past the live-update tail, under 99% of
    /// the rows embedded.
    pub fn gap(&self) -> bool {
        self.partial() && (self.embedded as f64) < self.total as f64 * 0.99
    }
}
pub fn has_schema(c: &Connection) -> Result<bool> {
    Ok(c.query_row(
        "SELECT 1 FROM sqlite_master WHERE name='semantic_queue'",
        [],
        |_| Ok(()),
    )
    .optional()?
    .is_some())
}
pub fn initialize(root: &DataRoot) -> Result<()> {
    initialize_with(root, false)
}
pub fn initialize_wait(root: &DataRoot) -> Result<()> {
    initialize_with(root, true)
}
fn initialize_with(root: &DataRoot, blocking: bool) -> Result<()> {
    let reader = store::reader(root)?;
    let counted = reader.query_row(
        "SELECT EXISTS(SELECT 1 FROM state WHERE key='semantic_ref_count')",
        [],
        |r| r.get::<_, bool>(0),
    )?;
    if has_schema(&reader)? && counted {
        return Ok(());
    }
    drop(reader);
    let Some(_lock) = root.lock(blocking)? else {
        return Ok(());
    };
    let c = store::writer(root)?;
    c.execute_batch("BEGIN IMMEDIATE;
CREATE INDEX IF NOT EXISTS prose_model ON prose(model);
CREATE TABLE IF NOT EXISTS semantic_queue(row_id INTEGER PRIMARY KEY,digest TEXT NOT NULL,ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS semantic_queue_time ON semantic_queue(ts DESC);
CREATE TABLE IF NOT EXISTS semantic_refs(row_id INTEGER PRIMARY KEY,digest TEXT NOT NULL,content_key TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS semantic_refs_content ON semantic_refs(content_key);
CREATE TABLE IF NOT EXISTS semantic_blocks(name TEXT PRIMARY KEY,rows INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_chunks(content_key TEXT NOT NULL,ordinal INTEGER NOT NULL,block TEXT NOT NULL,slot INTEGER NOT NULL,PRIMARY KEY(content_key,ordinal)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS semantic_chunks_location ON semantic_chunks(block,slot);
INSERT OR IGNORE INTO state SELECT 'semantic_ref_count',COUNT(*) FROM semantic_refs;
CREATE TRIGGER IF NOT EXISTS semantic_ref_insert AFTER INSERT ON semantic_refs BEGIN
UPDATE state SET value=CAST(value AS INTEGER)+1 WHERE key='semantic_ref_count'; END;
CREATE TRIGGER IF NOT EXISTS semantic_ref_delete AFTER DELETE ON semantic_refs BEGIN
UPDATE state SET value=CAST(value AS INTEGER)-1 WHERE key='semantic_ref_count'; END;
CREATE TRIGGER IF NOT EXISTS semantic_insert AFTER INSERT ON prose WHEN new.visible=1 BEGIN
INSERT OR REPLACE INTO semantic_queue VALUES(new.id,new.digest,new.ts); END;
CREATE TRIGGER IF NOT EXISTS semantic_update AFTER UPDATE OF text,digest,visible,who,agent ON prose BEGIN
DELETE FROM semantic_refs WHERE row_id=new.id;
DELETE FROM semantic_queue WHERE row_id=new.id;
INSERT INTO semantic_queue SELECT new.id,new.digest,new.ts WHERE new.visible=1; END;
CREATE TRIGGER IF NOT EXISTS semantic_delete AFTER DELETE ON prose BEGIN
DELETE FROM semantic_refs WHERE row_id=old.id; DELETE FROM semantic_queue WHERE row_id=old.id; END;
INSERT OR IGNORE INTO semantic_queue SELECT id,digest,ts FROM prose WHERE visible=1 AND NOT EXISTS(SELECT 1 FROM semantic_refs r WHERE r.row_id=prose.id AND r.digest=prose.digest);
COMMIT;")?;
    Ok(())
}
/// Invalidate all derived vectors after an explicitly requested full rebuild.
pub fn reset(root: &DataRoot) -> Result<()> {
    let _work = root.work_lock(true)?;
    initialize_wait(root)?;
    let _lock = root.lock(true)?;
    let mut c = store::writer(root)?;
    let tx = c.transaction()?;
    tx.execute_batch("DELETE FROM semantic_refs; DELETE FROM semantic_chunks; DELETE FROM semantic_blocks; DELETE FROM semantic_queue; INSERT INTO semantic_queue SELECT id,digest,ts FROM prose WHERE visible=1;")?;
    tx.commit()?;
    Ok(())
}
pub fn coverage(c: &Connection) -> Result<Coverage> {
    let total = c.query_row(
        "SELECT COALESCE((SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_count'),0)",
        [],
        |r| r.get(0),
    )?;
    let embedded = if has_schema(c)? {
        c.query_row(
            "SELECT CAST(value AS INTEGER) FROM state WHERE key='semantic_ref_count'",
            [],
            |r| r.get(0),
        )?
    } else {
        0
    };
    Ok(Coverage { embedded, total })
}
pub fn pending(c: &Connection) -> Result<bool> {
    if !has_schema(c)? {
        return Ok(false);
    }
    Ok(
        c.query_row("SELECT EXISTS(SELECT 1 FROM semantic_queue)", [], |r| {
            r.get(0)
        })?,
    )
}
#[derive(Debug)]
pub struct Pending {
    pub id: i64,
    pub digest: String,
    pub key: String,
    pub chunks: Vec<String>,
}
pub fn batch(c: &Connection, n: usize) -> Result<Vec<Pending>> {
    let mut s=c.prepare("SELECT p.id,p.digest,p.agent,p.who,p.text FROM semantic_queue q JOIN prose p ON p.id=q.row_id AND p.digest=q.digest WHERE p.visible=1 ORDER BY q.ts DESC,q.row_id DESC LIMIT ?")?;
    let rows = s.query_map([n], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, String>(3)?,
            r.get::<_, Vec<u8>>(4)?,
        ))
    })?;
    let mut out = vec![];
    for row in rows {
        let (id, digest, agent, who, bytes) = row?;
        let text = store::decode("prose", bytes)?;
        if store::digest(&text) != digest {
            bail!("semantic source digest mismatch");
        }
        // The raw digest plus preparation variant binds ALL chunks to the source text.
        let key = format!(
            "{}:{}",
            if agent == "pi" && who == "recap" {
                "recap"
            } else {
                "text"
            },
            digest
        );
        out.push(Pending {
            id,
            digest,
            key,
            chunks: semantic_model::chunks(&agent, &who, &text),
        });
    }
    Ok(out)
}
pub fn known(c: &Connection, key: &str) -> Result<bool> {
    Ok(c.query_row(
        "SELECT 1 FROM semantic_chunks WHERE content_key=? LIMIT 1",
        [key],
        |_| Ok(()),
    )
    .optional()?
    .is_some())
}
pub fn quantize(vector: &[f32]) -> Result<(Vec<i8>, f32)> {
    if vector.len() != DIM || vector.iter().any(|x| !x.is_finite()) {
        bail!("invalid semantic vector");
    }
    let peak = vector.iter().fold(0f32, |a, b| a.max(b.abs()));
    if peak == 0. {
        bail!("zero semantic vector");
    }
    let scale = peak / 127.;
    Ok((
        vector
            .iter()
            .map(|v| (v / scale).round().clamp(-127., 127.) as i8)
            .collect(),
        scale,
    ))
}
pub fn publish(
    root: &DataRoot,
    pending: &[Pending],
    new: &[(String, usize)],
    vectors: &[Vec<f32>],
) -> Result<usize> {
    if new.len() != vectors.len() {
        bail!("semantic batch length mismatch");
    }
    // File name derives from its exact bytes; a crash can only leave an unreferenced block.
    let mut bytes = Vec::with_capacity(vectors.len() * (DIM * 3 + 4));
    for v in vectors {
        let (q, scale) = quantize(v)?;
        bytes.extend_from_slice(&scale.to_le_bytes());
        bytes.extend(q.into_iter().map(|x| x as u8));
        for x in v {
            bytes.extend_from_slice(&half::f16::from_f32(*x).to_le_bytes());
        }
    }
    use sha2::{Digest, Sha256};
    let name = format!("{}.q8f16", crate::store::hex_digest(Sha256::digest(&bytes)));
    if !new.is_empty() {
        let dir = root.path().join("vectors");
        fs::create_dir_all(&dir)?;
        let path = dir.join(&name);
        let stage = dir.join(format!("{name}.pending"));
        if path.exists() {
            if fs::read(&path)? != bytes {
                bail!("semantic block checksum mismatch");
            }
        } else {
            let mut file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&stage)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            drop(file);
            fs::rename(&stage, &path)?;
        }
        #[cfg(unix)]
        File::open(&dir)?.sync_all()?;
    }
    let _lock = root.lock(true)?;
    let mut c = store::writer(root)?;
    let tx = c.transaction()?;
    if !new.is_empty() {
        tx.execute(
            "INSERT OR IGNORE INTO semantic_blocks VALUES(?,?)",
            params![name, new.len()],
        )?;
        let mut s = tx.prepare("INSERT OR IGNORE INTO semantic_chunks VALUES(?,?,?,?)")?;
        for (slot, (key, ordinal)) in new.iter().enumerate() {
            s.execute(params![key, ordinal, name, slot])?;
        }
    }
    let mut attached = 0;
    for p in pending {
        attached+=tx.execute("INSERT INTO semantic_refs SELECT id,digest,?1 FROM prose WHERE id=?2 AND digest=?3 AND visible=1 AND (CASE WHEN agent='pi' AND who='recap' THEN 'recap:' ELSE 'text:' END)||digest=?1 ON CONFLICT(row_id) DO UPDATE SET digest=excluded.digest,content_key=excluded.content_key",params![p.key,p.id,p.digest])?;
        tx.execute(
            "DELETE FROM semantic_queue WHERE row_id=?1 AND digest=?2 AND EXISTS(SELECT 1 FROM prose WHERE id=?1 AND (CASE WHEN agent='pi' AND who='recap' THEN 'recap:' ELSE 'text:' END)||digest=?3)",
            params![p.id, p.digest, p.key],
        )?;
    }
    tx.commit()?;
    Ok(attached)
}
pub struct Block {
    pub name: String,
    pub map: memmap2::Mmap,
    pub rows: usize,
}
const RECORD: usize = DIM * 3 + 4;
impl Block {
    /// Rows whose quantized upper bound reaches `floor`, with that bound and the
    /// exact F16 cosine.
    pub fn candidates(
        &self,
        query: &[i8],
        query_scale: f32,
        exact_query: &[f32],
        floor: f32,
    ) -> Vec<(usize, f32, f32)> {
        let root_dim = (DIM as f32).sqrt();
        let query_error = root_dim * query_scale * 0.5;
        let record = |i: usize| &self.map[i * RECORD..(i + 1) * RECORD];
        let mut found: Vec<(usize, f32, f32)> = (0..self.rows)
            .filter_map(|i| {
                let r = record(i);
                let row_scale = f32::from_le_bytes(r[..4].try_into().unwrap());
                let row = unsafe { std::slice::from_raw_parts(r[4..].as_ptr().cast::<i8>(), DIM) };
                let score = crate::semantic_q8::dot(row, query) as f32 * row_scale * query_scale;
                // Cauchy-Schwarz bounds both int8 quantization errors. The extra
                // 0.002 covers normalized IEEE f16 rounding and F32 accumulation.
                let upper =
                    score + query_error + (1. + query_error) * root_dim * row_scale * 0.5 + 0.002;
                (upper >= floor).then_some((i, upper, 0.))
            })
            .collect();
        for group in found.chunks_mut(LANES) {
            // A short final group repeats its last row; the extra lanes are dropped.
            let rows = std::array::from_fn(|j| &record(group[j.min(group.len() - 1)].0)[DIM + 4..]);
            for (candidate, exact) in group.iter_mut().zip(exact_cosines(rows, exact_query)) {
                candidate.2 = exact;
            }
        }
        found
    }
}
const LANES: usize = 8;
/// Cosines of stored F16 vectors with the query, one row per lane. Every lane
/// accumulates in F32 in dimension order without fused multiply-adds, so each
/// result equals the row-at-a-time loop bit for bit; the lanes only add parallelism.
fn exact_cosines(rows: [&[u8]; LANES], query: &[f32]) -> [f32; LANES] {
    let mut dot = [0f32; LANES];
    let mut norm = [0f32; LANES];
    for (k, q) in query.iter().enumerate() {
        let column: [f32; LANES] = std::array::from_fn(|j| {
            half::f16::from_le_bytes([rows[j][2 * k], rows[j][2 * k + 1]]).to_f32()
        });
        for ((d, n), v) in dot.iter_mut().zip(&mut norm).zip(column) {
            *d += v * q;
            *n += v * v;
        }
    }
    std::array::from_fn(|j| dot[j] / norm[j].sqrt())
}
/// The snapshot's published blocks in name order; only this step needs the connection.
pub fn block_list(c: &Connection) -> Result<Vec<(String, usize)>> {
    if !has_schema(c)? {
        return Ok(vec![]);
    }
    let mut s = c.prepare("SELECT name,rows FROM semantic_blocks ORDER BY name")?;
    let values = s
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, usize>(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(values)
}
/// Fewer blocks than this open on the calling thread: handing a few opens to the
/// Rayon pool costs more than the opens, and waits for a worker to be scheduled.
const PARALLEL_OPENS: usize = 32;
pub fn open_blocks(root: &DataRoot, listed: Vec<(String, usize)>) -> Result<Vec<Block>> {
    use rayon::prelude::*;
    let open = |(name, rows): (String, usize)| {
        if name.len() != 70
            || !name.ends_with(".q8f16")
            || !name[..64].bytes().all(|b| b.is_ascii_hexdigit())
        {
            bail!("invalid semantic block name");
        }
        let f = File::open(root.path().join("vectors").join(&name))
            .context("read semantic vector block")?;
        if f.metadata()?.len() as usize != rows * RECORD {
            bail!("invalid semantic vector block length");
        }
        // Published blocks are immutable and remain valid while any SQLite reader uses them.
        let map = unsafe { memmap2::MmapOptions::new().map(&f)? };
        #[cfg(unix)]
        let _ = map.advise(memmap2::Advice::WillNeed);
        Ok(Block { name, map, rows })
    };
    if listed.len() < PARALLEL_OPENS {
        listed.into_iter().map(open).collect()
    } else {
        listed.into_par_iter().map(open).collect()
    }
}
pub fn blocks(root: &DataRoot, c: &Connection) -> Result<Vec<Block>> {
    open_blocks(root, block_list(c)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    // Cases are 0.4.9's own (py/test_surface_policy.py:807-821) and its silent `-s` on the
    // 24-row meaning fixture: complete coverage and a churn-sized lag say nothing; a
    // rebuild or a small corpus missing over a quarter of its rows is a gap.
    #[test]
    fn coverage_names_a_gap_as_0_4_9() {
        let gap = |embedded, total| Coverage { embedded, total }.gap();
        assert!(!gap(24, 24));
        assert!(!gap(0, 0));
        assert!(!gap(46358, 46421));
        assert!(gap(4, 7200));
        assert!(gap(20, 28));
        assert!(gap(0, 1));
    }
    #[test]
    fn parallel_block_mapping_preserves_order_and_validates_paths() {
        let (_t, r) = root();
        fs::create_dir_all(r.path().join("vectors")).unwrap();
        let c = store::writer(&r).unwrap();
        for letter in ['b', 'a'] {
            let name = format!("{}.q8f16", letter.to_string().repeat(64));
            fs::write(r.path().join("vectors").join(&name), vec![0; DIM * 3 + 4]).unwrap();
            c.execute("INSERT INTO semantic_blocks VALUES(?,1)", [name])
                .unwrap();
        }
        let mapped = blocks(&r, &c).unwrap();
        assert!(mapped[0].name.starts_with('a'));
        assert!(mapped[1].name.starts_with('b'));
        c.execute("INSERT INTO semantic_blocks VALUES('../invalid',1)", [])
            .unwrap();
        assert!(blocks(&r, &c)
            .err()
            .unwrap()
            .to_string()
            .contains("invalid semantic block name"));
    }
    #[test]
    fn lane_cosines_equal_the_row_at_a_time_loop_bit_for_bit() {
        let mut state = 0x9e37_79b9_7f4a_7c15u64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            let unit = (state >> 40) as f32 / (1u64 << 24) as f32 * 2. - 1.;
            // Subnormal and large F16 components as well as ordinary ones.
            unit * [1e-6, 9., 1., 1., 0.1][(state % 5) as usize]
        };
        let query: Vec<f32> = (0..DIM).map(|_| next()).collect();
        let rows = LANES + 3;
        let mut bytes = Vec::new();
        let mut expected = Vec::new();
        for _ in 0..rows {
            let vector: Vec<f32> = (0..DIM).map(|_| next()).collect();
            let (q, scale) = quantize(&vector).unwrap();
            bytes.extend_from_slice(&scale.to_le_bytes());
            bytes.extend(q.into_iter().map(|x| x as u8));
            let (mut dot, mut norm) = (0f32, 0f32);
            for (x, q) in vector.iter().zip(&query) {
                let v = half::f16::from_f32(*x);
                bytes.extend_from_slice(&v.to_le_bytes());
                dot += v.to_f32() * q;
                norm += v.to_f32() * v.to_f32();
            }
            expected.push(dot / norm.sqrt());
        }
        let mut map = memmap2::MmapMut::map_anon(bytes.len()).unwrap();
        map.copy_from_slice(&bytes);
        let block = Block {
            name: String::new(),
            map: map.make_read_only().unwrap(),
            rows,
        };
        let (q, scale) = quantize(&query).unwrap();
        let found = block.candidates(&q, scale, &query, f32::NEG_INFINITY);
        assert_eq!(
            found.iter().map(|c| c.0).collect::<Vec<_>>(),
            (0..rows).collect::<Vec<_>>()
        );
        for (candidate, expected) in found.iter().zip(&expected) {
            assert_eq!(candidate.2.to_bits(), expected.to_bits());
        }
    }
    fn root() -> (tempfile::TempDir, DataRoot) {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path().join("data"), true).unwrap();
        store::writer(&root).unwrap();
        initialize(&root).unwrap();
        (t, root)
    }
    fn row(root: &DataRoot, text: &str) {
        let c = store::writer(root).unwrap();
        c.execute("INSERT INTO prose(id,source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra) VALUES(1,1,'codex','abc','project',1,'user','',0,5,?,?,'')",params![text.as_bytes(),store::digest(text)]).unwrap();
    }
    #[test]
    fn coverage_preserves_numeric_output_contract() {
        let value = serde_json::to_value(Coverage {
            embedded: 3,
            total: 4,
        })
        .unwrap();
        assert_eq!(value["indexed"], 3);
        assert_eq!(value["pending"], 1);
        assert_eq!(value["fraction"], 0.75);
        assert_eq!(value["complete"], false);
        assert_eq!(value["order"], "newest-first");
    }
    #[test]
    fn inference_cannot_attach_to_edited_text() {
        let (_t, r) = root();
        row(&r, "old");
        let p = batch(&store::reader(&r).unwrap(), 8).unwrap();
        let c = store::writer(&r).unwrap();
        c.execute(
            "UPDATE prose SET text=?,digest=? WHERE id=1",
            params![b"new".as_slice(), store::digest("new")],
        )
        .unwrap();
        let mut v = vec![0f32; DIM];
        v[0] = 1.;
        assert_eq!(publish(&r, &p, &[(p[0].key.clone(), 0)], &[v]).unwrap(), 0);
        assert_eq!(coverage(&store::reader(&r).unwrap()).unwrap().embedded, 0);
        assert!(pending(&store::reader(&r).unwrap()).unwrap());
    }
    #[test]
    fn preparation_change_during_inference_keeps_new_work_queued() {
        let (_t, r) = root();
        row(&r, "Resume instructions\nFILES\n=====\nwork");
        let p = batch(&store::reader(&r).unwrap(), 8).unwrap();
        store::writer(&r)
            .unwrap()
            .execute("UPDATE prose SET agent='pi',who='recap' WHERE id=1", [])
            .unwrap();
        let mut v = vec![0f32; DIM];
        v[0] = 1.;
        assert_eq!(publish(&r, &p, &[(p[0].key.clone(), 0)], &[v]).unwrap(), 0);
        assert!(pending(&store::reader(&r).unwrap()).unwrap());
    }
    #[test]
    fn reference_published_after_block_and_deleted_with_row() {
        let (_t, r) = root();
        row(&r, "hello");
        let p = batch(&store::reader(&r).unwrap(), 8).unwrap();
        let mut v = vec![0f32; DIM];
        v[5] = 1.;
        publish(&r, &p, &[(p[0].key.clone(), 0)], &[v]).unwrap();
        let c = store::reader(&r).unwrap();
        assert_eq!(coverage(&c).unwrap().embedded, 1);
        assert!(!pending(&c).unwrap());
        assert_eq!(blocks(&r, &c).unwrap().len(), 1);
        drop(c);
        store::writer(&r)
            .unwrap()
            .execute("DELETE FROM prose", [])
            .unwrap();
        assert_eq!(coverage(&store::reader(&r).unwrap()).unwrap().embedded, 0);
    }
    #[test]
    fn reference_count_is_transactional_and_upserts_do_not_double_count() {
        let (_t, r) = root();
        row(&r, "old");
        let pending = batch(&store::reader(&r).unwrap(), 8).unwrap();
        let mut vector = vec![0.; DIM];
        vector[0] = 1.;
        let new = vec![(pending[0].key.clone(), 0)];
        publish(&r, &pending, &new, std::slice::from_ref(&vector)).unwrap();
        publish(&r, &pending, &new, std::slice::from_ref(&vector)).unwrap();
        initialize(&r).unwrap();
        let snapshot = store::reader(&r).unwrap();
        assert_eq!(coverage(&snapshot).unwrap().embedded, 1);
        let mut writer = store::writer(&r).unwrap();
        {
            let tx = writer.transaction().unwrap();
            tx.execute("DELETE FROM semantic_refs", []).unwrap();
            assert_eq!(coverage(&tx).unwrap().embedded, 0);
            tx.rollback().unwrap();
        }
        assert_eq!(coverage(&store::reader(&r).unwrap()).unwrap().embedded, 1);
        writer
            .execute(
                "UPDATE prose SET text=?,digest=? WHERE id=1",
                params![b"new".as_slice(), store::digest("new")],
            )
            .unwrap();
        assert_eq!(coverage(&store::reader(&r).unwrap()).unwrap().embedded, 0);
        assert_eq!(coverage(&snapshot).unwrap().embedded, 1);
        drop(snapshot);
        let pending = batch(&store::reader(&r).unwrap(), 8).unwrap();
        publish(&r, &pending, &[(pending[0].key.clone(), 0)], &[vector]).unwrap();
        assert_eq!(coverage(&store::reader(&r).unwrap()).unwrap().embedded, 1);
        writer.execute("DELETE FROM prose WHERE id=1", []).unwrap();
        assert_eq!(coverage(&store::reader(&r).unwrap()).unwrap().embedded, 0);
    }
}

/// Called under work.lock; old snapshots have ten minutes to finish with removed blocks.
pub fn reclaim(root: &DataRoot) -> Result<usize> {
    let referenced = {
        let _lock = root.lock(true)?;
        let mut c = store::writer(root)?;
        if !has_schema(&c)? {
            return Ok(0);
        }
        let tx = c.transaction()?;
        tx.execute("DELETE FROM semantic_chunks WHERE NOT EXISTS(SELECT 1 FROM semantic_refs r WHERE r.content_key=semantic_chunks.content_key)", [])?;
        tx.execute("DELETE FROM semantic_blocks WHERE NOT EXISTS(SELECT 1 FROM semantic_chunks c WHERE c.block=semantic_blocks.name)", [])?;
        let names = tx
            .prepare("SELECT name FROM semantic_blocks")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<std::collections::HashSet<_>>>()?;
        tx.commit()?;
        names
    };
    let dir = root.path().join("vectors");
    let entries = match fs::read_dir(dir) {
        Ok(v) => v,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(e.into()),
    };
    let cutoff = std::time::SystemTime::now() - std::time::Duration::from_secs(600);
    let mut deleted = 0;
    for entry in entries {
        let entry = entry?;
        if entry.file_type()?.is_file()
            && !referenced.contains(&entry.file_name().to_string_lossy().into_owned())
            && entry.metadata()?.modified()? < cutoff
        {
            fs::remove_file(entry.path())?;
            deleted += 1;
        }
    }
    Ok(deleted)
}
#[cfg(test)]
mod reclamation_tests {
    use super::*;
    #[test]
    fn retains_referenced_and_recent_vectors_and_reclaims_old_orphans() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path().join("data"), true).unwrap();
        store::writer(&root).unwrap();
        initialize(&root).unwrap();
        let dir = root.path().join("vectors");
        fs::create_dir(&dir).unwrap();
        for name in ["held", "recent", "orphan"] {
            fs::write(dir.join(name), b"vector").unwrap();
        }
        for name in ["held", "orphan"] {
            File::options()
                .write(true)
                .open(dir.join(name))
                .unwrap()
                .set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(601))
                .unwrap();
        }
        let c = store::writer(&root).unwrap();
        c.execute("INSERT INTO semantic_refs VALUES(1,'digest','key')", [])
            .unwrap();
        c.execute("INSERT INTO semantic_chunks VALUES('key',0,'held',0)", [])
            .unwrap();
        c.execute("INSERT INTO semantic_blocks VALUES('held',1)", [])
            .unwrap();
        assert_eq!(reclaim(&root).unwrap(), 1);
        assert!(dir.join("held").exists());
        assert!(dir.join("recent").exists());
        assert!(!dir.join("orphan").exists());
    }
}
