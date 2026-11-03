//! A source census without parsing. Directory listings are reused until their mtime changes.
use crate::{
    data::DataRoot,
    ingest::registry::{self, ADAPTERS},
    store::{self, Stamp},
};
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::{Connection, Transaction};
use serde::Serialize;
use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
    time::Instant,
};

#[derive(Clone)]
pub(crate) struct Directory {
    path: String,
    agent: String,
    mtime: i64,
    dirs: Arc<Vec<String>>,
    files: Arc<Vec<String>>,
    dirty: bool,
}
pub(crate) struct Census {
    pub stamps: Vec<Stamp>,
    directories: Vec<Directory>,
    removed: Vec<String>,
    listing_ms: f64,
    stat_ms: f64,
}
#[derive(Default, Debug, Serialize)]
pub struct Pending {
    pub changed_sources: usize,
    pub census_ms: f64,
    pub read_ms: f64,
    pub listing_ms: f64,
    pub stat_ms: f64,
    pub comparison_ms: f64,
}
fn encode_names(names: &[String]) -> Vec<u8> {
    let mut out = Vec::new();
    for name in names {
        out.extend((name.len() as u32).to_le_bytes());
        out.extend(name.as_bytes());
    }
    out
}
fn decode_names(mut bytes: &[u8]) -> Result<Vec<String>> {
    let mut out = vec![];
    while !bytes.is_empty() {
        anyhow::ensure!(bytes.len() >= 4, "invalid cached directory listing");
        let length = u32::from_le_bytes(bytes[..4].try_into()?) as usize;
        bytes = &bytes[4..];
        anyhow::ensure!(length <= bytes.len(), "invalid cached directory entry");
        out.push(std::str::from_utf8(&bytes[..length])?.to_string());
        bytes = &bytes[length..];
    }
    Ok(out)
}
fn mtime(m: &fs::Metadata) -> i64 {
    m.modified()
        .ok()
        .and_then(crate::intake_batch::epoch_ns)
        .unwrap_or(0)
}
pub(crate) fn scan(c: &Connection, force: bool) -> Result<Census> {
    let started = Instant::now();
    let mut cached = HashMap::new();
    if !force {
        let mut stmt = c.prepare("SELECT path,agent,mtime,dirs,files FROM source_directories")?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let d = Directory {
                path: row.get(0)?,
                agent: row.get(1)?,
                mtime: row.get(2)?,
                dirs: Arc::new(decode_names(&row.get::<_, Vec<u8>>(3)?)?),
                files: Arc::new(decode_names(&row.get::<_, Vec<u8>>(4)?)?),
                dirty: false,
            };
            cached.insert(d.path.clone(), d);
        }
    }
    let mut queue: Vec<_> = ADAPTERS
        .iter()
        .flat_map(|a| a.freshness_roots().into_iter().map(|p| (a.name(), p)))
        .collect();
    let mut seen = HashSet::new();
    let mut directories = Vec::new();
    let mut files = Vec::new();
    let mut issues = Vec::new();
    while !queue.is_empty() {
        queue.retain(|(_, p)| seen.insert(p.clone()));
        let batch: Vec<_> = queue
            .par_iter()
            .map(|(agent, path)| -> Result<Option<Directory>> {
                let meta = match fs::symlink_metadata(path) {
                    Ok(m) => m,
                    Err(e) if registry::stat_proves_absent(path, &e) => return Ok(None),
                    Err(e) => return Err(e.into()),
                };
                if registry::metadata_is_link(&meta) {
                    return Ok(None);
                }
                let a = ADAPTERS.iter().find(|a| a.name() == *agent).unwrap();
                if meta.is_file() {
                    return Ok(Some(Directory {
                        path: path.to_string_lossy().into(),
                        agent: (*agent).into(),
                        mtime: mtime(&meta),
                        dirs: Arc::default(),
                        files: Arc::new(if a.freshness_content(path) {
                            vec![String::new()]
                        } else {
                            vec![]
                        }),
                        dirty: true,
                    }));
                }
                if !meta.is_dir() {
                    return Ok(None);
                }
                if let Some(d) = cached
                    .get(path.to_string_lossy().as_ref())
                    .filter(|d| d.mtime == mtime(&meta))
                {
                    return Ok(Some(d.clone()));
                }
                let mut d = Directory {
                    path: path.to_string_lossy().into(),
                    agent: (*agent).into(),
                    mtime: mtime(&meta),
                    dirs: Arc::default(),
                    files: Arc::default(),
                    dirty: true,
                };
                for entry in fs::read_dir(path)? {
                    let entry = entry?;
                    let ty = entry.file_type()?;
                    let p = entry.path();
                    if ty.is_dir() {
                        Arc::make_mut(&mut d.dirs).push(entry.file_name().to_string_lossy().into());
                    } else if ty.is_file() && a.freshness_content(&p) {
                        Arc::make_mut(&mut d.files)
                            .push(entry.file_name().to_string_lossy().into());
                    }
                }
                Ok(Some(d))
            })
            .collect();
        let inputs = std::mem::take(&mut queue);
        // Each result carries its agent; failures retain a named root error below.
        for ((agent, path), result) in inputs.into_iter().zip(batch) {
            match result {
                Ok(Some(d)) => {
                    let a = ADAPTERS
                        .iter()
                        .find(|a| a.name() == d.agent)
                        .unwrap()
                        .name();
                    queue.extend(d.dirs.iter().map(|p| {
                        (
                            a,
                            if p.is_empty() {
                                PathBuf::from(&d.path)
                            } else {
                                Path::new(&d.path).join(p)
                            },
                        )
                    }));
                    files.extend(d.files.iter().map(|p| {
                        (
                            a,
                            if p.is_empty() {
                                PathBuf::from(&d.path)
                            } else {
                                Path::new(&d.path).join(p)
                            },
                        )
                    }));
                    directories.push(d);
                }
                Ok(None) => {}
                Err(e) => issues.push(Stamp {
                    path: path.to_string_lossy().into(),
                    agent: agent.into(),
                    size: 0,
                    mtime_ns: 0,
                    token: String::new(),
                    error: Some(e.to_string()),
                }),
            }
        }
    }
    let listing_ms = started.elapsed().as_secs_f64() * 1000.;
    let stat_start = Instant::now();
    let mut stamps: Vec<_> = files
        .par_iter()
        .filter_map(|(a, p)| store::stamp_present(a, p))
        .collect();
    let stat_ms = stat_start.elapsed().as_secs_f64() * 1000.;
    // Directory errors must not license deletion of an unseen subtree.
    stamps.extend(issues);
    stamps.sort_unstable_by(|a, b| a.path.cmp(&b.path));
    stamps.dedup_by(|a, b| a.path == b.path);
    let names: HashSet<_> = directories.iter().map(|d| d.path.as_str()).collect();
    let removed = cached
        .keys()
        .filter(|p| !names.contains(p.as_str()))
        .cloned()
        .collect();
    let directories = directories.into_iter().filter(|d| d.dirty).collect();
    Ok(Census {
        stamps,
        directories,
        removed,
        listing_ms,
        stat_ms,
    })
}
pub(crate) fn save(tx: &Transaction<'_>, census: &Census) -> Result<()> {
    for path in &census.removed {
        tx.execute("DELETE FROM source_directories WHERE path=?", [path])?;
    }
    let mut stmt = tx.prepare_cached("INSERT INTO source_directories VALUES(?1,?2,?3,?4,?5) ON CONFLICT(path) DO UPDATE SET agent=excluded.agent,mtime=excluded.mtime,dirs=excluded.dirs,files=excluded.files")?;
    for d in &census.directories {
        stmt.execute(rusqlite::params![
            d.path,
            d.agent,
            d.mtime,
            encode_names(&d.dirs),
            encode_names(&d.files)
        ])?;
    }
    Ok(())
}
pub fn pending(root: &DataRoot) -> Result<Pending> {
    let start = Instant::now();
    let c = store::reader(root)?;
    let read_ms = start.elapsed().as_secs_f64() * 1000.;
    let census = scan(&c, false)?;
    let mut old = c.prepare("SELECT path,size,mtime_ns,change_token FROM sources ORDER BY path")?;
    let mut rows = old.query([])?;
    let (mut next, mut changed) = (0, 0);
    while let Some(row) = rows.next()? {
        let path = row.get_ref(0)?.as_str()?;
        if path.starts_with('@') || path.starts_with("token:") {
            continue;
        }
        while next < census.stamps.len() && census.stamps[next].path.as_str() < path {
            changed += 1;
            next += 1;
        }
        if let Some(stamp) = census.stamps.get(next).filter(|s| s.path == path) {
            if stamp.size != row.get::<_, u64>(1)?
                || stamp.mtime_ns != row.get::<_, i64>(2)?
                || stamp.token != row.get_ref(3)?.as_str()?
            {
                changed += 1;
            }
            next += 1;
        } else if matches!(fs::symlink_metadata(path),Err(e) if registry::stat_proves_absent(Path::new(path),&e))
        {
            changed += 1;
        }
    }
    changed += census.stamps.len() - next;
    let census_ms = start.elapsed().as_secs_f64() * 1000.;
    Ok(Pending {
        changed_sources: changed,
        census_ms,
        read_ms,
        listing_ms: census.listing_ms,
        stat_ms: census.stat_ms,
        comparison_ms: (census_ms - read_ms - census.listing_ms - census.stat_ms).max(0.),
    })
}
/// Spawn only an eligible one-shot worker. Kernel locks arbitrate concurrent spawns.
pub fn spawn(root: &DataRoot, binary: &Path) -> Result<bool> {
    let Some(guard) = root.work_lock(false)? else {
        return Ok(false);
    };
    let mut cmd = Command::new(binary);
    cmd.arg("__work")
        .env("AGREP_DATA_DIR", root.path())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(crate::logging::open(root)?);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::getpriority(libc::PRIO_PROCESS, 0) < 10
                    && libc::setpriority(libc::PRIO_PROCESS, 0, 10) < 0
                {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x00000008 | 0x00000200);
    }
    drop(guard);
    cmd.spawn()?;
    Ok(true)
}
