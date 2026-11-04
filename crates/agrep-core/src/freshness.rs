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
    sync::Arc,
    time::Instant,
};

// Eight stat workers avoided the contention of the machine-wide Rayon pool
// in the real-history sweep; smaller machines retain their available width.
static STAT_POOL: std::sync::LazyLock<rayon::ThreadPool> = std::sync::LazyLock::new(|| {
    rayon::ThreadPoolBuilder::new()
        .num_threads(
            std::thread::available_parallelism()
                .map_or(1, usize::from)
                .min(8),
        )
        .build()
        .expect("source stat thread pool")
});
/// Sources per stat task within one directory.
const STAT_CHUNK: usize = 64;

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
    listed_directories: usize,
    cached_directories: usize,
    stat_files: usize,
}
#[derive(Default, Debug, Serialize)]
pub struct Pending {
    pub changed_sources: usize,
    pub census_ms: f64,
    pub read_ms: f64,
    pub listing_ms: f64,
    pub stat_ms: f64,
    pub comparison_ms: f64,
    pub listed_directories: usize,
    pub cached_directories: usize,
    pub stat_files: usize,
    /// FTS segments need a merge, for instance one a killed worker left unfinished.
    pub merge_pending: bool,
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
    let cached = if force {
        HashMap::new()
    } else {
        cached_directories(c)?
    };
    Ok(walk(cached, started.elapsed().as_secs_f64() * 1000.))
}
fn cached_directories(c: &Connection) -> Result<HashMap<String, Directory>> {
    let mut cached = HashMap::new();
    let mut stmt = c.prepare("SELECT path,agent,mtime,dirs,files FROM source_directories")?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let d = Directory {
            path: row.get(0)?,
            agent: row.get(1)?,
            mtime: row.get(2)?,
            dirs: Arc::new(decode_names(row.get_ref(3)?.as_blob()?)?),
            files: Arc::new(decode_names(row.get_ref(4)?.as_blob()?)?),
            dirty: false,
        };
        cached.insert(d.path.clone(), d);
    }
    Ok(cached)
}
/// Each directory's sources are stat'ed in the same task that confirmed its listing,
/// so the walk is one parallel pass per directory level.
fn walk(cached: HashMap<String, Directory>, listing_ms: f64) -> Census {
    let started = Instant::now();
    let mut queue: Vec<_> = ADAPTERS
        .iter()
        .flat_map(|a| a.freshness_roots().into_iter().map(|p| (a.name(), p)))
        .collect();
    let mut seen = HashSet::new();
    let mut directories = Vec::new();
    let mut stamps = Vec::new();
    let mut issues = Vec::new();
    let listings = std::sync::atomic::AtomicUsize::new(0);
    let cache_hits = std::sync::atomic::AtomicUsize::new(0);
    let stat_files = std::sync::atomic::AtomicUsize::new(0);
    while !queue.is_empty() {
        queue.retain(|(_, p)| seen.insert(p.clone()));
        let batch: Vec<_> = STAT_POOL.install(|| {
            queue
                .par_iter()
                .map(|(agent, path)| -> Result<Option<(Directory, Vec<Stamp>)>> {
                    let Some(d) = directory(&cached, agent, path, &listings, &cache_hits)? else {
                        return Ok(None);
                    };
                    let owner = ADAPTERS
                        .iter()
                        .find(|a| a.name() == d.agent)
                        .unwrap()
                        .name();
                    stat_files.fetch_add(d.files.len(), std::sync::atomic::Ordering::Relaxed);
                    // A flat store holds thousands of sources in one directory; its stats
                    // split across the pool, while a small directory stays one task.
                    let files = d
                        .files
                        .par_iter()
                        .with_min_len(STAT_CHUNK)
                        .filter_map(|name| {
                            if name.is_empty() {
                                store::stamp_present(owner, Path::new(&d.path))
                            } else {
                                store::stamp_present(owner, &Path::new(&d.path).join(name))
                            }
                        })
                        .collect();
                    Ok(Some((d, files)))
                })
                .collect()
        });
        let inputs = std::mem::take(&mut queue);
        // Each result carries its agent; failures retain a named root error below.
        for ((agent, path), result) in inputs.into_iter().zip(batch) {
            match result {
                Ok(Some((d, files))) => {
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
                    stamps.extend(files);
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
    let stat_ms = started.elapsed().as_secs_f64() * 1000.;
    // Directory errors must not license deletion of an unseen subtree.
    stamps.extend(issues);
    STAT_POOL.install(|| stamps.par_sort_unstable_by(|a, b| a.path.cmp(&b.path)));
    stamps.dedup_by(|a, b| a.path == b.path);
    let names: HashSet<_> = directories.iter().map(|d| d.path.as_str()).collect();
    let removed = cached
        .keys()
        .filter(|p| !names.contains(p.as_str()))
        .cloned()
        .collect();
    let directories = directories.into_iter().filter(|d| d.dirty).collect();
    Census {
        stamps,
        directories,
        removed,
        listing_ms,
        stat_ms,
        listed_directories: listings.into_inner(),
        cached_directories: cache_hits.into_inner(),
        stat_files: stat_files.into_inner(),
    }
}
/// The current listing of one freshness root or directory: reused while its mtime
/// is unchanged, otherwise read again.
fn directory(
    cached: &HashMap<String, Directory>,
    agent: &str,
    path: &Path,
    listings: &std::sync::atomic::AtomicUsize,
    cache_hits: &std::sync::atomic::AtomicUsize,
) -> Result<Option<Directory>> {
    let meta = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if registry::stat_proves_absent(path, &e) => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    if registry::metadata_is_link(&meta) {
        return Ok(None);
    }
    let a = ADAPTERS.iter().find(|a| a.name() == agent).unwrap();
    if meta.is_file() {
        return Ok(Some(Directory {
            path: path.to_string_lossy().into(),
            agent: agent.into(),
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
        cache_hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        return Ok(Some(d.clone()));
    }
    let mut d = Directory {
        path: path.to_string_lossy().into(),
        agent: agent.into(),
        mtime: mtime(&meta),
        dirs: Arc::default(),
        files: Arc::default(),
        dirty: true,
    };
    listings.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        let p = entry.path();
        if ty.is_dir() {
            Arc::make_mut(&mut d.dirs).push(entry.file_name().to_string_lossy().into());
        } else if ty.is_file() && a.freshness_content(&p) {
            Arc::make_mut(&mut d.files).push(entry.file_name().to_string_lossy().into());
        }
    }
    Ok(Some(d))
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
type Fingerprint = (String, u64, i64, String);
/// The index's fingerprint of every file source, in path order.
fn indexed(c: &Connection) -> Result<Vec<Fingerprint>> {
    let mut old = c.prepare("SELECT path,size,mtime_ns,change_token FROM sources ORDER BY path")?;
    let rows = old.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?;
    let mut out = Vec::new();
    for row in rows {
        let row: Fingerprint = row?;
        if !row.0.starts_with('@') && !row.0.starts_with("token:") {
            out.push(row);
        }
    }
    Ok(out)
}
pub fn pending(root: &DataRoot) -> Result<Pending> {
    let start = Instant::now();
    let c = store::reader(root)?;
    let read_ms = start.elapsed().as_secs_f64() * 1000.;
    let listing = Instant::now();
    let cached = cached_directories(&c)?;
    let listing_ms = listing.elapsed().as_secs_f64() * 1000.;
    // The walk is syscall-bound; the index's fingerprints are read meanwhile.
    let (census, old) = STAT_POOL.install(|| {
        rayon::join(
            move || walk(cached, listing_ms),
            move || -> Result<_> { Ok((indexed(&c)?, crate::fts_segments::pending(&c)?)) },
        )
    });
    let (old, merge_pending) = old?;
    let (mut next, mut changed) = (0, 0);
    for (path, size, mtime_ns, token) in &old {
        while next < census.stamps.len() && census.stamps[next].path < *path {
            changed += 1;
            next += 1;
        }
        if let Some(stamp) = census.stamps.get(next).filter(|s| s.path == *path) {
            if stamp.size != *size || stamp.mtime_ns != *mtime_ns || stamp.token != *token {
                changed += 1;
            }
            next += 1;
        } else if store::gone(Path::new(path)) {
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
        listed_directories: census.listed_directories,
        cached_directories: census.cached_directories,
        stat_files: census.stat_files,
        merge_pending,
    })
}
/// Spawn only an eligible one-shot worker. Kernel locks arbitrate concurrent spawns.
pub fn spawn(root: &DataRoot, binary: &Path) -> Result<bool> {
    let Some(guard) = root.work_lock(false)? else {
        return Ok(false);
    };
    detached(root, binary, &["__work"], guard)
}
/// Spawn a merging worker unless a process already merges; callers spawn it only when
/// [`crate::fts_segments::pending`] reports merge work.
pub fn spawn_merge(root: &DataRoot, binary: &Path) -> Result<bool> {
    let Some(guard) = root.merge_lock()? else {
        return Ok(false);
    };
    detached(root, binary, &["__work", "merge"], guard)
}
/// The worker gets null stdin and stdout, the log as stderr, and no other descriptor or
/// handle of this process: a worker holding a pipe this process got from its caller
/// (`out=$(agrep search x 3>&1)`) would keep a caller that reads to the end waiting for it.
fn detached(
    root: &DataRoot,
    binary: &Path,
    args: &[&str],
    guard: crate::data::WriterLock,
) -> Result<bool> {
    let log = crate::logging::open(root)?;
    drop(guard);
    let pid = spawn_worker(binary, args, root.path(), log)?;
    crate::semantic_profile::event(serde_json::json!({"worker_started":{"pid":pid}}));
    Ok(true)
}
#[cfg(unix)]
fn spawn_worker(binary: &Path, args: &[&str], data: &Path, log: fs::File) -> Result<u32> {
    use std::{
        os::unix::process::CommandExt,
        process::{Command, Stdio},
    };
    let mut cmd = Command::new(binary);
    cmd.args(args)
        .env("AGREP_DATA_DIR", data)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log)
        .process_group(0);
    // Its own process group keeps terminal signals from the worker. A pre_exec hook, such
    // as closefrom in the child, would force fork+exec and make the command wait until the
    // child is scheduled and execs; without one std uses posix_spawn. So the inheritable
    // descriptors above stderr are close-on-exec only while it spawns. __work lowers its
    // own priority.
    let listing = if cfg!(target_os = "linux") {
        "/proc/self/fd"
    } else {
        "/dev/fd"
    };
    let mut marked = vec![];
    for entry in fs::read_dir(listing)? {
        let Some(fd) = entry?.file_name().to_str().and_then(|n| n.parse().ok()) else {
            continue;
        };
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if fd > 2
            && flags >= 0
            && flags & libc::FD_CLOEXEC == 0
            && unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } == 0
        {
            marked.push((fd, flags));
        }
    }
    let child = cmd.spawn();
    for (fd, flags) in marked {
        unsafe { libc::fcntl(fd, libc::F_SETFD, flags) };
    }
    Ok(child?.id())
}
/// CreateProcess passes a child every inheritable handle in this process unless a handle
/// list names the only ones to inherit.
#[cfg(windows)]
fn spawn_worker(binary: &Path, args: &[&str], data: &Path, log: fs::File) -> Result<u32> {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt, os::windows::io::AsRawHandle};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT},
        System::Threading::{
            CreateProcessW, DeleteProcThreadAttributeList, InitializeProcThreadAttributeList,
            UpdateProcThreadAttribute, CREATE_NEW_PROCESS_GROUP, CREATE_UNICODE_ENVIRONMENT,
            DETACHED_PROCESS, EXTENDED_STARTUPINFO_PRESENT, PROCESS_INFORMATION,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES, STARTUPINFOEXW,
        },
    };
    let null = fs::OpenOptions::new().read(true).write(true).open("NUL")?;
    let handles = [
        null.as_raw_handle() as HANDLE,
        log.as_raw_handle() as HANDLE,
    ];
    for handle in handles {
        if unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) } == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    let wide = |s: &OsStr| s.encode_wide().collect::<Vec<u16>>();
    let application: Vec<u16> = wide(binary.as_os_str()).into_iter().chain([0]).collect();
    let mut line = vec![u16::from(b'"')];
    line.extend(wide(binary.as_os_str()));
    line.push(u16::from(b'"'));
    for arg in args {
        line.push(u16::from(b' '));
        line.extend(arg.encode_utf16());
    }
    line.push(0);
    let mut variables: Vec<_> = std::env::vars_os()
        .filter(|(key, _)| !key.eq_ignore_ascii_case("AGREP_DATA_DIR"))
        .collect();
    variables.push(("AGREP_DATA_DIR".into(), data.as_os_str().to_owned()));
    // Windows keeps an environment block sorted by name, ignoring case.
    variables.sort_by_key(|(key, _)| key.to_string_lossy().to_uppercase());
    let mut environment = vec![];
    for (key, value) in &variables {
        environment.extend(wide(key));
        environment.push(u16::from(b'='));
        environment.extend(wide(value));
        environment.push(0);
    }
    environment.push(0);
    let mut size = 0;
    unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), 1, 0, &mut size) };
    let mut buffer = vec![0usize; size.div_ceil(std::mem::size_of::<usize>())];
    let list = buffer.as_mut_ptr().cast();
    if unsafe { InitializeProcThreadAttributeList(list, 1, 0, &mut size) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let mut info: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    info.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    info.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    info.StartupInfo.hStdInput = handles[0];
    info.StartupInfo.hStdOutput = handles[0];
    info.StartupInfo.hStdError = handles[1];
    info.lpAttributeList = list;
    let mut process: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let created = unsafe {
        UpdateProcThreadAttribute(
            list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            handles.as_ptr().cast(),
            std::mem::size_of_val(&handles),
            std::ptr::null_mut(),
            std::ptr::null(),
        ) != 0
            && CreateProcessW(
                application.as_ptr(),
                line.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                1,
                CREATE_NEW_PROCESS_GROUP
                    | DETACHED_PROCESS
                    | EXTENDED_STARTUPINFO_PRESENT
                    | CREATE_UNICODE_ENVIRONMENT,
                environment.as_ptr().cast(),
                std::ptr::null(),
                &info.StartupInfo,
                &mut process,
            ) != 0
    };
    let error = std::io::Error::last_os_error();
    unsafe { DeleteProcThreadAttributeList(list) };
    if !created {
        return Err(error.into());
    }
    unsafe {
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
    }
    Ok(process.dwProcessId)
}
