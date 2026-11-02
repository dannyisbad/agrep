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
        serde_json::json!({"indexed":self.embedded,"total":self.total,"pending":self.total.saturating_sub(self.embedded),"fraction":if self.total==0 {1.0}else{self.embedded as f64/self.total as f64},"complete":!self.partial(),"order":"newest-first"}).serialize(serializer)
    }
}
impl Coverage {
    pub fn partial(&self) -> bool {
        self.embedded < self.total
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
    if has_schema(&store::reader(root)?)? {
        return Ok(());
    }
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
pub fn coverage(c: &Connection) -> Result<Coverage> {
    let total = c.query_row(
        "SELECT COALESCE((SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_count'),0)",
        [],
        |r| r.get(0),
    )?;
    let embedded = if has_schema(c)? {
        c.query_row("SELECT COUNT(*) FROM semantic_refs", [], |r| r.get(0))?
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
    let name = format!("{:x}.q8f16", Sha256::digest(&bytes));
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
        attached+=tx.execute("INSERT OR REPLACE INTO semantic_refs SELECT id,digest,?1 FROM prose WHERE id=?2 AND digest=?3 AND visible=1 AND (CASE WHEN agent='pi' AND who='recap' THEN 'recap:' ELSE 'text:' END)||digest=?1",params![p.key,p.id,p.digest])?;
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
impl Block {
    pub fn exact_score(&self, slot: usize, query: &[f32]) -> f32 {
        let start = slot * (DIM * 3 + 4) + DIM + 4;
        let mut dot = 0f32;
        let mut norm = 0f32;
        for (bytes, q) in self.map[start..start + DIM * 2].chunks_exact(2).zip(query) {
            let v = half::f16::from_le_bytes([bytes[0], bytes[1]]).to_f32();
            dot += v * q;
            norm += v * v;
        }
        dot / norm.sqrt()
    }
    pub fn candidates(&self, query: &[i8], query_scale: f32, floor: f32) -> Vec<(usize, f32)> {
        let root_dim = (DIM as f32).sqrt();
        let query_error = root_dim * query_scale * 0.5;
        self.map
            .chunks_exact(DIM * 3 + 4)
            .enumerate()
            .filter_map(|(i, r)| {
                let row_scale = f32::from_le_bytes(r[..4].try_into().unwrap());
                let row = unsafe { std::slice::from_raw_parts(r[4..].as_ptr().cast::<i8>(), DIM) };
                let score = crate::semantic_q8::dot(row, query) as f32 * row_scale * query_scale;
                // Cauchy-Schwarz bounds both int8 quantization errors. The extra
                // 0.002 covers normalized IEEE f16 rounding and F32 accumulation.
                let upper =
                    score + query_error + (1. + query_error) * root_dim * row_scale * 0.5 + 0.002;
                (upper >= floor).then_some((i, upper))
            })
            .collect()
    }
}
pub fn blocks(root: &DataRoot, c: &Connection) -> Result<Vec<Block>> {
    if !has_schema(c)? {
        return Ok(vec![]);
    }
    let mut s = c.prepare("SELECT name,rows FROM semantic_blocks ORDER BY name")?;
    let values = s.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, usize>(1)?)))?;
    let mut out = vec![];
    for value in values {
        let (name, rows) = value?;
        if name.len() != 70
            || !name.ends_with(".q8f16")
            || !name[..64].bytes().all(|b| b.is_ascii_hexdigit())
        {
            bail!("invalid semantic block name");
        }
        let f = File::open(root.path().join("vectors").join(&name))
            .context("read semantic vector block")?;
        if f.metadata()?.len() as usize != rows * (DIM * 3 + 4) {
            bail!("invalid semantic vector block length");
        }
        // Published blocks are immutable and remain valid while any SQLite reader uses them.
        let map = unsafe { memmap2::MmapOptions::new().map(&f)? };
        out.push(Block { name, map, rows });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
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
