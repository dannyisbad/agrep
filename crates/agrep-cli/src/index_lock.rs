use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const PROTOCOL: &str = "index-lock-v1";
pub const TIMEOUT_MS: u64 = 1_800_000;
pub const PUBLICATION_GRACE_MS: u64 = 3_000;
pub const RETRY_INITIAL_MS: u64 = 50;
pub const RETRY_FACTOR_NUMERATOR: u32 = 3;
pub const RETRY_FACTOR_DENOMINATOR: u32 = 2;
pub const RETRY_MAX_MS: u64 = 1_000;
pub const WAIT_NOTICE_MS: u64 = 1_000;
pub const WAIT_REPEAT_MS: u64 = 30_000;
pub const MAX_RECORD_BYTES: usize = 4_096;
pub const POSIX_MAX_PID: u32 = 0x7fff_ffff;
pub const WINDOWS_MAX_PID: u32 = 0xffff_ffff;

#[cfg(windows)]
pub const MAX_PID: u32 = WINDOWS_MAX_PID;
#[cfg(not(windows))]
pub const MAX_PID: u32 = POSIX_MAX_PID;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Owner {
    pub pid: u32,
    pub start: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct FileIdentity {
    primary: u64,
    secondary: u64,
    size: u64,
}

#[derive(Debug, Clone)]
struct Snapshot {
    identity: FileIdentity,
    modified: SystemTime,
    raw: Vec<u8>,
}

#[derive(Debug, Clone)]
pub(crate) struct LiveHolder {
    snapshot: Snapshot,
}

impl Snapshot {
    fn matches(&self, other: &Self) -> bool {
        self.identity == other.identity && self.raw == other.raw
    }
}

impl FileIdentity {
    fn same_entry(&self, other: &Self) -> bool {
        self.primary == other.primary && self.secondary == other.secondary
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Liveness {
    Alive,
    Dead,
    Unknown,
}

pub fn pid_alive_for_cleanup(pid: u32) -> Option<bool> {
    match process_liveness(pid) {
        Liveness::Alive => Some(true),
        Liveness::Dead => Some(false),
        Liveness::Unknown => None,
    }
}

pub(crate) fn current_process_start_identity() -> Option<String> {
    process_start_identity(std::process::id())
}

pub(crate) fn observe_live_holder(path: &Path, label: &str) -> io::Result<Option<LiveHolder>> {
    let observed = match snapshot(path) {
        Ok(observed) => observed,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let Some(owner) = parse_owner(&observed.raw) else {
        return Ok(None);
    };
    let Some(start) = owner.start.as_deref().filter(|start| *start != "unknown") else {
        return Ok(None);
    };
    if record_field(&observed.raw, "label") != Some(label)
        || process_liveness(owner.pid) != Liveness::Alive
        || process_start_identity(owner.pid).as_deref() != Some(start)
    {
        return Ok(None);
    }
    Ok(Some(LiveHolder { snapshot: observed }))
}

pub(crate) fn live_holder_unchanged(path: &Path, holder: &LiveHolder) -> bool {
    let Ok(observed) = snapshot(path) else {
        return false;
    };
    if !observed.matches(&holder.snapshot) {
        return false;
    }
    let Some(owner) = parse_owner(&observed.raw) else {
        return false;
    };
    let Some(start) = owner.start.as_deref().filter(|start| *start != "unknown") else {
        return false;
    };
    process_liveness(owner.pid) == Liveness::Alive
        && process_start_identity(owner.pid).as_deref() == Some(start)
}

fn process_identity_reused(expected: Option<&str>, actual: Option<&str>) -> bool {
    matches!(
        (expected, actual),
        (Some(expected), Some(actual))
            if expected != "unknown"
                && expected != actual
    )
}

fn is_derived_adoption_claim(raw: &[u8]) -> bool {
    let Some(body) = record_text(raw) else {
        return false;
    };
    let (mut state, mut pid, mut start, mut writer, mut token) = (None, None, None, None, None);
    for part in body.split_ascii_whitespace() {
        let Some((key, value)) = part.split_once('=') else {
            return false;
        };
        let slot = match key {
            "state" => &mut state,
            "pid" => &mut pid,
            "start" => &mut start,
            "writer" => &mut writer,
            "token" => &mut token,
            _ => return false,
        };
        if value.is_empty() || slot.replace(value).is_some() {
            return false;
        }
    }
    state == Some("derived-adoption")
        && pid.is_some()
        && start.is_some()
        && writer.is_some_and(|value| {
            value.len() == 20
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        && token.is_some_and(|value| validate_token(value).is_ok())
}

pub(crate) fn reclaim_dead_owner_record(path: &Path) -> io::Result<bool> {
    let observed = snapshot(path)?;
    if !is_derived_adoption_claim(&observed.raw) {
        return Ok(false);
    }
    let Some(owner) = parse_owner(&observed.raw) else {
        return Ok(false);
    };
    let liveness = process_liveness(owner.pid);
    let actual_start = if liveness == Liveness::Alive {
        process_start_identity(owner.pid)
    } else {
        None
    };
    let reused = process_identity_reused(owner.start.as_deref(), actual_start.as_deref());
    Ok((liveness == Liveness::Dead || reused) && remove_exact(path, &observed))
}

pub struct IndexLock {
    path: PathBuf,
    file: Option<File>,
    snapshot: Snapshot,
    released: bool,
}

impl IndexLock {
    pub fn acquire(path: &Path, label: &str, timeout: Duration) -> anyhow::Result<Self> {
        validate_label(label)?;
        let started = Instant::now();
        let debug = debug_enabled();
        let mut next_notice = Duration::from_millis(WAIT_NOTICE_MS);
        let mut public_notice_sent = false;
        let mut delay = Duration::from_millis(RETRY_INITIAL_MS);
        let mut holder = "an unreadable owner record".to_owned();
        loop {
            match create(path, label) {
                Ok(lock) => return Ok(lock),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                    if let Ok(observed) = snapshot(path) {
                        holder = holder_description(&observed.raw);
                    }
                    if reclaimable(path)? {
                        continue;
                    }
                    let elapsed = started.elapsed();
                    if elapsed >= timeout {
                        if debug {
                            anyhow::bail!(
                                "timed out waiting for {} held by {holder}",
                                path.display()
                            );
                        }
                        anyhow::bail!("{}", public_timeout_error(&holder));
                    }
                    if elapsed >= next_notice {
                        let remaining = timeout.saturating_sub(elapsed);
                        if debug {
                            eprintln!(
                                "waiting for {} held by {holder}; timeout in {:.0}s",
                                path.display(),
                                remaining.as_secs_f64()
                            );
                        } else if should_emit_public_wait_notice(&mut public_notice_sent) {
                            eprintln!("{}", public_wait_notice(&holder));
                        }
                        next_notice = elapsed + Duration::from_millis(WAIT_REPEAT_MS);
                    }
                    std::thread::sleep(delay);
                    delay = delay
                        .mul_f64(RETRY_FACTOR_NUMERATOR as f64 / RETRY_FACTOR_DENOMINATOR as f64)
                        .min(Duration::from_millis(RETRY_MAX_MS));
                }
                Err(error) => {
                    return Err(anyhow::anyhow!(
                        "could not create index lock {}: {error}",
                        path.display()
                    ));
                }
            }
        }
    }

    pub fn release(&mut self) -> anyhow::Result<()> {
        if self.released {
            return Ok(());
        }
        self.file.take();
        if remove_exact(&self.path, &self.snapshot) {
            self.released = true;
            return Ok(());
        }
        match find_owned_record(&self.path, &self.snapshot) {
            Ok(Some(stranded)) => {
                anyhow::bail!("could not release owned index lock: {}", stranded.display());
            }
            Ok(None) => {}
            Err(error) => {
                return Err(anyhow::anyhow!(
                    "could not verify index lock release {}: {error}",
                    self.path.display()
                ));
            }
        }
        self.released = true;
        Ok(())
    }
}

impl Drop for IndexLock {
    fn drop(&mut self) {
        if self.released {
            return;
        }
        self.file.take();
        self.released = true;
        let _ = remove_exact(&self.path, &self.snapshot);
    }
}

pub fn render_record(
    pid: u32,
    start: &str,
    token: &str,
    label: &str,
    time_ms: u64,
) -> anyhow::Result<Vec<u8>> {
    anyhow::ensure!(valid_pid(pid), "pid must be in 1..{MAX_PID}");
    validate_start(start)?;
    validate_token(token)?;
    validate_label(label)?;
    let seconds = time_ms / 1_000;
    let millis = time_ms % 1_000;
    let raw =
        format!("pid={pid} start={start} token={token} label={label} time={seconds}.{millis:03}\n")
            .into_bytes();
    anyhow::ensure!(
        raw.len() <= MAX_RECORD_BYTES,
        "index lock record exceeds {MAX_RECORD_BYTES} bytes"
    );
    Ok(raw)
}

pub fn parse_owner(raw: &[u8]) -> Option<Owner> {
    let body = record_text(raw)?;
    let mut pid = None;
    let mut start = None;
    for part in body.split_ascii_whitespace() {
        if let Some(value) = part.strip_prefix("pid=") {
            if pid.is_some() {
                return None;
            }
            pid = Some(value);
        } else if let Some(value) = part.strip_prefix("start=") {
            if start.is_some() || value.is_empty() {
                return None;
            }
            start = Some(value);
        }
    }
    let pid = pid?;
    if pid.is_empty() || !pid.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let pid = pid.parse::<u64>().ok()?;
    if pid == 0 || pid > u64::from(MAX_PID) {
        return None;
    }
    Some(Owner {
        pid: pid as u32,
        start: start.map(str::to_owned),
    })
}

pub fn describe_contract() -> serde_json::Value {
    serde_json::json!({
        "schema": 1,
        "protocol": PROTOCOL,
        "canonical_fields": ["pid", "start", "token", "label", "time"],
        "constants": {
            "timeout_ms": TIMEOUT_MS,
            "publication_grace_ms": PUBLICATION_GRACE_MS,
            "retry_initial_ms": RETRY_INITIAL_MS,
            "retry_factor_numerator": RETRY_FACTOR_NUMERATOR,
            "retry_factor_denominator": RETRY_FACTOR_DENOMINATOR,
            "retry_max_ms": RETRY_MAX_MS,
            "wait_notice_ms": WAIT_NOTICE_MS,
            "wait_repeat_ms": WAIT_REPEAT_MS,
            "max_record_bytes": MAX_RECORD_BYTES,
            "max_pid": {
                "posix": POSIX_MAX_PID,
                "windows": WINDOWS_MAX_PID,
            },
        },
        "platform_max_pid": MAX_PID,
    })
}

fn create(path: &Path, label: &str) -> io::Result<IndexLock> {
    let pid = std::process::id();
    let start = process_start_identity(pid).unwrap_or_else(|| "unknown".into());
    let token = secure_token()?;
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| io::Error::other(format!("system clock before Unix epoch: {error}")))?;
    let time_ms = u64::try_from(elapsed.as_millis())
        .map_err(|_| io::Error::other("system time does not fit the lock protocol"))?;
    let raw = render_record(pid, &start, &token, label, time_ms)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    let mut options = OpenOptions::new();
    options.read(true).write(true).create_new(true);
    secure_create_options(&mut options);
    let mut file = options.open(path)?;
    let created_metadata = file.metadata()?;
    let created_identity = file_identity(&file, &created_metadata)?;
    if let Err(error) = set_exact_mode(&file) {
        drop(file);
        let _ = remove_created_partial(path, &created_identity, b"");
        return Err(error);
    }
    let mut written = 0;
    let write_result = (|| -> io::Result<()> {
        while written < raw.len() {
            let count = file.write(&raw[written..])?;
            if count == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::WriteZero,
                    format!("short write creating index lock {}", path.display()),
                ));
            }
            written += count;
        }
        file.flush()
    })();
    if let Err(error) = write_result {
        drop(file);
        let _ = remove_created_partial(path, &created_identity, &raw[..written]);
        return Err(error);
    }
    let observed = match snapshot(path) {
        Ok(observed) if observed.raw == raw && observed.identity.same_entry(&created_identity) => {
            observed
        }
        Ok(_) => {
            drop(file);
            let _ = remove_created_partial(path, &created_identity, &raw);
            return Err(io::Error::other(format!(
                "index lock changed while creating {}",
                path.display()
            )));
        }
        Err(error) => {
            drop(file);
            let _ = remove_created_partial(path, &created_identity, &raw);
            return Err(error);
        }
    };
    Ok(IndexLock {
        path: path.to_path_buf(),
        file: Some(file),
        snapshot: observed,
        released: false,
    })
}

fn reclaimable(path: &Path) -> anyhow::Result<bool> {
    let observed = match snapshot(path) {
        Ok(observed) => observed,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(_) => return Ok(false),
    };
    if let Some(owner) = parse_owner(&observed.raw) {
        let liveness = process_liveness(owner.pid);
        let actual_start = if liveness == Liveness::Alive {
            process_start_identity(owner.pid)
        } else {
            None
        };
        let reused = process_identity_reused(owner.start.as_deref(), actual_start.as_deref());
        return Ok((liveness == Liveness::Dead || reused) && remove_exact(path, &observed));
    }
    Ok(
        !publication_is_fresh(observed.modified, SystemTime::now())
            && remove_exact(path, &observed),
    )
}

fn publication_is_fresh(modified: SystemTime, now: SystemTime) -> bool {
    now.duration_since(modified)
        .is_ok_and(|age| age <= Duration::from_millis(PUBLICATION_GRACE_MS))
}

fn holder_description(raw: &[u8]) -> String {
    let Some(owner) = parse_owner(raw) else {
        return "an unparseable owner record".to_owned();
    };
    let label = record_text(raw).and_then(|body| {
        body.split_ascii_whitespace()
            .find_map(|part| part.strip_prefix("label="))
            .filter(|value| validate_label(value).is_ok())
    });
    match label {
        Some(label) => format!("pid={} label={label}", owner.pid),
        None => format!("pid={}", owner.pid),
    }
}

fn record_field<'a>(raw: &'a [u8], name: &str) -> Option<&'a str> {
    let mut found = record_text(raw)?
        .split_ascii_whitespace()
        .filter_map(|part| part.split_once('='))
        .filter_map(|(key, value)| (key == name && !value.is_empty()).then_some(value));
    let value = found.next()?;
    found.next().is_none().then_some(value)
}

fn public_wait_notice(holder: &str) -> &'static str {
    let fields: Vec<_> = holder.split_ascii_whitespace().collect();
    if holder_is_corpusdb(&fields) {
        "search database update is finishing; waiting..."
    } else if fields.contains(&"label=agrep-rs") {
        "another transcript index is finishing; waiting..."
    } else {
        "another index task is finishing; waiting..."
    }
}

fn public_timeout_error(holder: &str) -> &'static str {
    let fields: Vec<_> = holder.split_ascii_whitespace().collect();
    if holder_is_corpusdb(&fields) {
        "search database update did not finish; retry, or set AGREP_DEBUG=1 for details"
    } else if fields.contains(&"label=agrep-rs") {
        "another transcript index did not finish; retry, or set AGREP_DEBUG=1 for details"
    } else {
        "another index task did not finish; retry, or set AGREP_DEBUG=1 for details"
    }
}

fn holder_is_corpusdb(fields: &[&str]) -> bool {
    fields.iter().any(|field| {
        field
            .strip_prefix("label=")
            .is_some_and(|label| label == "corpusdb" || label.starts_with("corpusdb:"))
    })
}

fn should_emit_public_wait_notice(sent: &mut bool) -> bool {
    if *sent {
        false
    } else {
        *sent = true;
        true
    }
}

fn debug_enabled() -> bool {
    std::env::var_os("AGREP_DEBUG").is_some_and(|value| {
        !matches!(
            value.to_string_lossy().to_ascii_lowercase().as_str(),
            "" | "0" | "false" | "no" | "off"
        )
    })
}

fn record_text(raw: &[u8]) -> Option<&str> {
    if raw.is_empty()
        || raw.len() > MAX_RECORD_BYTES
        || !raw.ends_with(b"\n")
        || raw.iter().filter(|byte| **byte == b'\n').count() != 1
        || raw.contains(&0)
    {
        return None;
    }
    let body = std::str::from_utf8(&raw[..raw.len() - 1]).ok()?;
    Some(body.strip_suffix('\r').unwrap_or(body))
}

fn validate_label(label: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        !label.is_empty()
            && label.len() <= 64
            && label
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte)),
        "label must match [A-Za-z0-9._:-]{{1,64}}"
    );
    Ok(())
}

#[cfg(windows)]
fn valid_pid(pid: u32) -> bool {
    pid != 0
}

#[cfg(not(windows))]
fn valid_pid(pid: u32) -> bool {
    pid > 0 && pid <= POSIX_MAX_PID
}

fn validate_start(start: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        !start.is_empty()
            && start.len() <= 128
            && start.is_ascii()
            && start
                .bytes()
                .all(|byte| (b'!'..=b'~').contains(&byte) && byte != b'='),
        "start must be one nonempty ASCII field value"
    );
    Ok(())
}

fn validate_token(token: &str) -> anyhow::Result<()> {
    anyhow::ensure!(
        token.len() == 32
            && token
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)),
        "token must be exactly 32 lowercase hex characters"
    );
    Ok(())
}

fn secure_token() -> io::Result<String> {
    let mut random = [0u8; 16];
    fill_random(&mut random)?;
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut token = String::with_capacity(32);
    for byte in random {
        token.push(HEX[(byte >> 4) as usize] as char);
        token.push(HEX[(byte & 0x0f) as usize] as char);
    }
    Ok(token)
}

#[cfg(unix)]
fn fill_random(output: &mut [u8]) -> io::Result<()> {
    File::open("/dev/urandom")?.read_exact(output)
}

#[cfg(windows)]
fn fill_random(output: &mut [u8]) -> io::Result<()> {
    const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 2;
    #[link(name = "bcrypt")]
    extern "system" {
        fn BCryptGenRandom(
            algorithm: *mut core::ffi::c_void,
            buffer: *mut u8,
            length: u32,
            flags: u32,
        ) -> i32;
    }
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            output.as_mut_ptr(),
            output.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status == 0 {
        Ok(())
    } else {
        Err(io::Error::other(format!(
            "BCryptGenRandom failed with status 0x{status:08x}"
        )))
    }
}

#[cfg(not(any(unix, windows)))]
fn fill_random(_output: &mut [u8]) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "secure random bytes are unavailable on this platform",
    ))
}

fn snapshot(path: &Path) -> io::Result<Snapshot> {
    plain_metadata(path)?;
    let mut options = OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let mut file = options.open(path)?;
    let handle_before = file.metadata()?;
    ensure_plain_metadata(path, &handle_before)?;
    let handle_identity = file_identity(&file, &handle_before)?;
    if entry_identity(path)? != handle_identity {
        return Err(changed_error(path));
    }
    let mut raw = Vec::with_capacity(MAX_RECORD_BYTES.min(handle_before.len() as usize));
    (&mut file)
        .take((MAX_RECORD_BYTES + 1) as u64)
        .read_to_end(&mut raw)?;
    if raw.len() > MAX_RECORD_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "index lock record exceeds {MAX_RECORD_BYTES} bytes: {}",
                path.display()
            ),
        ));
    }
    let handle_after = file.metadata()?;
    ensure_plain_metadata(path, &handle_after)?;
    let identity_after = file_identity(&file, &handle_after)?;
    if handle_identity != identity_after {
        return Err(changed_error(path));
    }
    plain_metadata(path)?;
    if entry_identity(path)? != identity_after {
        return Err(changed_error(path));
    }
    Ok(Snapshot {
        identity: identity_after,
        modified: handle_after.modified()?,
        raw,
    })
}

fn changed_error(path: &Path) -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        format!("index lock changed while reading {}", path.display()),
    )
}

fn plain_metadata(path: &Path) -> io::Result<Metadata> {
    let metadata = fs::symlink_metadata(path)?;
    ensure_plain_metadata(path, &metadata)?;
    Ok(metadata)
}

fn ensure_plain_metadata(path: &Path, metadata: &Metadata) -> io::Result<()> {
    let reparse = is_reparse_point(metadata);
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() || reparse {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("index lock is not a plain regular file: {}", path.display()),
        ));
    }
    Ok(())
}

fn entry_identity(path: &Path) -> io::Result<FileIdentity> {
    let mut options = OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    ensure_plain_metadata(path, &metadata)?;
    file_identity(&file, &metadata)
}

#[cfg(unix)]
fn file_identity(_file: &File, metadata: &Metadata) -> io::Result<FileIdentity> {
    use std::os::unix::fs::MetadataExt;

    Ok(FileIdentity {
        primary: metadata.dev(),
        secondary: metadata.ino(),
        size: metadata.size(),
    })
}

#[cfg(windows)]
fn file_identity(file: &File, _metadata: &Metadata) -> io::Result<FileIdentity> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(FileIdentity {
        primary: u64::from(info.dwVolumeSerialNumber),
        secondary: (u64::from(info.nFileIndexHigh) << 32) | u64::from(info.nFileIndexLow),
        size: (u64::from(info.nFileSizeHigh) << 32) | u64::from(info.nFileSizeLow),
    })
}

#[cfg(not(any(unix, windows)))]
fn file_identity(_file: &File, metadata: &Metadata) -> io::Result<FileIdentity> {
    Ok(FileIdentity {
        primary: 0,
        secondary: metadata
            .modified()
            .ok()
            .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
            .map(|duration| duration.as_nanos().min(u128::from(u64::MAX)) as u64)
            .unwrap_or(0),
        size: metadata.len(),
    })
}

#[cfg(windows)]
fn is_reparse_point(metadata: &Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    metadata.file_attributes() & 0x400 != 0
}

#[cfg(not(windows))]
fn is_reparse_point(_metadata: &Metadata) -> bool {
    false
}

#[cfg(target_os = "linux")]
fn secure_read_options(options: &mut OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;

    options.custom_flags(0x20_000 | 0x800);
}

#[cfg(target_os = "macos")]
fn secure_read_options(options: &mut OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;

    options.custom_flags(0x100 | 0x4);
}

#[cfg(windows)]
fn secure_read_options(options: &mut OpenOptions) {
    use std::os::windows::fs::OpenOptionsExt;

    options.custom_flags(0x0020_0000);
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn secure_read_options(_options: &mut OpenOptions) {}

#[cfg(unix)]
fn secure_create_options(options: &mut OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;

    options.mode(0o600);
}

#[cfg(windows)]
fn secure_create_options(options: &mut OpenOptions) {
    use std::os::windows::fs::OpenOptionsExt;

    options.custom_flags(0x0020_0000);
}

#[cfg(not(any(unix, windows)))]
fn secure_create_options(_options: &mut OpenOptions) {}

#[cfg(unix)]
fn set_exact_mode(file: &File) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    file.set_permissions(fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn set_exact_mode(_file: &File) -> io::Result<()> {
    Ok(())
}

fn remove_created_partial(
    path: &Path,
    created_identity: &FileIdentity,
    expected_raw: &[u8],
) -> bool {
    let Ok(observed) = snapshot(path) else {
        return false;
    };
    if !observed.identity.same_entry(created_identity) || observed.raw != expected_raw {
        return false;
    }
    remove_exact(path, &observed)
}

fn remove_exact(path: &Path, expected: &Snapshot) -> bool {
    let Some(tomb) = fresh_tomb_path(path) else {
        return false;
    };
    if fs::rename(path, &tomb).is_err() {
        return false;
    }
    let moved = snapshot(&tomb).ok();
    if moved.as_ref().is_some_and(|moved| moved.matches(expected)) && fs::remove_file(&tomb).is_ok()
    {
        return true;
    }
    restore_tomb(path, &tomb);
    false
}

fn fresh_tomb_path(path: &Path) -> Option<PathBuf> {
    for _ in 0..3 {
        let token = secure_token().ok()?;
        let name = path.file_name()?.to_string_lossy();
        let tomb =
            path.with_file_name(format!(".{name}.owner-reap-{}-{token}", std::process::id()));
        match fs::symlink_metadata(&tomb) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Some(tomb),
            _ => {}
        }
    }
    None
}

fn restore_tomb(path: &Path, tomb: &Path) {
    if !matches!(
        fs::symlink_metadata(path),
        Err(error) if error.kind() == io::ErrorKind::NotFound
    ) {
        return;
    }
    if fs::hard_link(tomb, path).is_ok() {
        let _ = fs::remove_file(tomb);
    }
}

fn find_owned_record(path: &Path, expected: &Snapshot) -> io::Result<Option<PathBuf>> {
    match snapshot(path) {
        Ok(observed) if observed.matches(expected) => return Ok(Some(path.to_path_buf())),
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let Some(name) = path.file_name() else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("index lock path has no file name: {}", path.display()),
        ));
    };
    let name = name.to_string_lossy();
    let prefix = format!(".{name}.owner-reap-");
    let Some(parent) = path.parent() else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("index lock path has no parent: {}", path.display()),
        ));
    };
    for entry in fs::read_dir(parent)? {
        let entry = entry?;
        let candidate = entry.path();
        if !entry.file_name().to_string_lossy().starts_with(&prefix) {
            continue;
        }
        match snapshot(&candidate) {
            Ok(observed) if observed.matches(expected) => return Ok(Some(candidate)),
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(None)
}

#[cfg(windows)]
fn process_liveness(pid: u32) -> Liveness {
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const ERROR_ACCESS_DENIED: u32 = 5;
    const STILL_ACTIVE: u32 = 259;
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut core::ffi::c_void;
        fn GetExitCodeProcess(handle: *mut core::ffi::c_void, code: *mut u32) -> i32;
        fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
        fn GetLastError() -> u32;
    }
    if pid == 0 {
        return Liveness::Dead;
    }
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return match GetLastError() {
                ERROR_ACCESS_DENIED => Liveness::Unknown,
                87 => Liveness::Dead,
                _ => Liveness::Unknown,
            };
        }
        let mut code = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        CloseHandle(handle);
        if ok == 0 {
            Liveness::Unknown
        } else if code == STILL_ACTIVE {
            Liveness::Alive
        } else {
            Liveness::Dead
        }
    }
}

#[cfg(target_os = "linux")]
fn process_liveness(pid: u32) -> Liveness {
    match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(raw) => raw
            .get(raw.rfind(')').map(|index| index + 2).unwrap_or(raw.len())..)
            .and_then(|tail| tail.split_ascii_whitespace().next())
            .map_or_else(
                || posix_kill_liveness(pid),
                |state| {
                    if matches!(state, "Z" | "X" | "x") {
                        Liveness::Dead
                    } else {
                        Liveness::Alive
                    }
                },
            ),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Liveness::Dead,
        Err(_) => posix_kill_liveness(pid),
    }
}

#[cfg(target_os = "macos")]
fn process_liveness(pid: u32) -> Liveness {
    if pid == 0 || pid > POSIX_MAX_PID {
        return Liveness::Dead;
    }
    let mut info = ProcBsdShortInfo::default();
    let size = std::mem::size_of::<ProcBsdShortInfo>() as i32;
    let got = unsafe {
        proc_pidinfo(
            pid as i32,
            PROC_PIDTBSD_SHORTINFO,
            0,
            (&mut info as *mut ProcBsdShortInfo).cast(),
            size,
        )
    };
    if got == size {
        if info.status == 5 {
            Liveness::Dead
        } else {
            Liveness::Alive
        }
    } else {
        darwin_failed_observation(pid)
    }
}

#[cfg(target_os = "macos")]
fn darwin_failed_observation(pid: u32) -> Liveness {
    extern "C" {
        fn getpgid(pid: i32) -> i32;
    }
    if unsafe { getpgid(pid as i32) } >= 0 {
        return Liveness::Unknown;
    }
    if io::Error::last_os_error().raw_os_error() != Some(3) {
        return Liveness::Unknown;
    }
    match posix_kill_liveness(pid) {
        Liveness::Alive | Liveness::Dead => Liveness::Dead,
        Liveness::Unknown => Liveness::Unknown,
    }
}

#[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
fn process_liveness(pid: u32) -> Liveness {
    posix_kill_liveness(pid)
}

#[cfg(not(any(unix, windows)))]
fn process_liveness(_pid: u32) -> Liveness {
    Liveness::Unknown
}

#[cfg(unix)]
fn posix_kill_liveness(pid: u32) -> Liveness {
    extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }
    if pid == 0 || pid > POSIX_MAX_PID {
        return Liveness::Dead;
    }
    if unsafe { kill(pid as i32, 0) } == 0 {
        return Liveness::Alive;
    }
    match io::Error::last_os_error().raw_os_error() {
        Some(3) => Liveness::Dead,
        Some(1) => Liveness::Unknown,
        _ => Liveness::Unknown,
    }
}

#[cfg(windows)]
fn process_start_identity(pid: u32) -> Option<String> {
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    #[repr(C)]
    struct FileTime {
        low: u32,
        high: u32,
    }
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut core::ffi::c_void;
        fn GetProcessTimes(
            handle: *mut core::ffi::c_void,
            created: *mut FileTime,
            exited: *mut FileTime,
            kernel: *mut FileTime,
            user: *mut FileTime,
        ) -> i32;
        fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
    }
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        let mut created = FileTime { low: 0, high: 0 };
        let mut exited = FileTime { low: 0, high: 0 };
        let mut kernel = FileTime { low: 0, high: 0 };
        let mut user = FileTime { low: 0, high: 0 };
        let ok = GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user);
        CloseHandle(handle);
        (ok != 0).then(|| {
            format!(
                "win_{}",
                (u64::from(created.high) << 32) | u64::from(created.low)
            )
        })
    }
}

#[cfg(target_os = "linux")]
fn process_start_identity(pid: u32) -> Option<String> {
    let raw = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let tail = raw.get(raw.rfind(')')? + 2..)?;
    Some(format!("proc_{}", tail.split_ascii_whitespace().nth(19)?))
}

#[cfg(target_os = "macos")]
const PROC_PIDTBSDINFO: i32 = 3;
#[cfg(target_os = "macos")]
const PROC_PIDTBSD_SHORTINFO: i32 = 13;

#[cfg(target_os = "macos")]
#[repr(C)]
#[derive(Default)]
struct ProcBsdShortInfo {
    pid: u32,
    ppid: u32,
    pgid: u32,
    status: u32,
    comm: [u8; 16],
    flags: u32,
    uid: u32,
    gid: u32,
    ruid: u32,
    rgid: u32,
    svuid: u32,
    svgid: u32,
    reserved: u32,
}

#[cfg(target_os = "macos")]
#[repr(C)]
#[derive(Default)]
struct ProcBsdInfo {
    pbi_flags: u32,
    pbi_status: u32,
    pbi_xstatus: u32,
    pbi_pid: u32,
    pbi_ppid: u32,
    pbi_uid: u32,
    pbi_gid: u32,
    pbi_ruid: u32,
    pbi_rgid: u32,
    pbi_svuid: u32,
    pbi_svgid: u32,
    rfu_1: u32,
    pbi_comm: [u8; 16],
    pbi_name: [u8; 32],
    pbi_nfiles: u32,
    pbi_pgid: u32,
    pbi_pjobc: u32,
    e_tdev: u32,
    e_tpgid: u32,
    pbi_nice: i32,
    pbi_start_tvsec: u64,
    pbi_start_tvusec: u64,
}

#[cfg(target_os = "macos")]
#[link(name = "proc")]
extern "C" {
    fn proc_pidinfo(
        pid: i32,
        flavor: i32,
        arg: u64,
        buffer: *mut core::ffi::c_void,
        buffersize: i32,
    ) -> i32;
}

#[cfg(target_os = "macos")]
fn process_start_identity(pid: u32) -> Option<String> {
    if pid > POSIX_MAX_PID {
        return None;
    }
    let mut info = ProcBsdInfo::default();
    let size = std::mem::size_of::<ProcBsdInfo>() as i32;
    let got = unsafe {
        proc_pidinfo(
            pid as i32,
            PROC_PIDTBSDINFO,
            0,
            (&mut info as *mut ProcBsdInfo).cast(),
            size,
        )
    };
    if got == size && info.pbi_start_tvsec != 0 {
        return Some(format!(
            "darwin_{}_{}",
            info.pbi_start_tvsec, info.pbi_start_tvusec
        ));
    }
    let mut short = ProcBsdShortInfo::default();
    let short_size = std::mem::size_of::<ProcBsdShortInfo>() as i32;
    let got = unsafe {
        proc_pidinfo(
            pid as i32,
            PROC_PIDTBSD_SHORTINFO,
            0,
            (&mut short as *mut ProcBsdShortInfo).cast(),
            short_size,
        )
    };
    if got == short_size {
        darwin_cross_uid_identity(short.uid)
    } else {
        None
    }
}

#[cfg(target_os = "macos")]
fn darwin_cross_uid_identity(uid: u32) -> Option<String> {
    extern "C" {
        fn geteuid() -> u32;
    }
    let current_uid = unsafe { geteuid() };
    (uid != current_uid).then(|| format!("darwin_uid_{uid}"))
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn process_start_identity(_pid: u32) -> Option<String> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> serde_json::Value {
        serde_json::from_str(include_str!(
            "../../../py/fixtures/index_lock_conformance.json"
        ))
        .unwrap()
    }

    fn temp_dir(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "agrep-index-lock-{name}-{}-{}",
            std::process::id(),
            secure_token().unwrap()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn decode_base64(encoded: &str) -> Vec<u8> {
        fn value(byte: u8) -> Option<u8> {
            match byte {
                b'A'..=b'Z' => Some(byte - b'A'),
                b'a'..=b'z' => Some(byte - b'a' + 26),
                b'0'..=b'9' => Some(byte - b'0' + 52),
                b'+' => Some(62),
                b'/' => Some(63),
                _ => None,
            }
        }

        let mut decoded = Vec::new();
        let mut accumulator = 0u32;
        let mut bits = 0u32;
        for byte in encoded.bytes().filter(|byte| *byte != b'=') {
            accumulator = (accumulator << 6) | u32::from(value(byte).unwrap());
            bits += 6;
            if bits >= 8 {
                bits -= 8;
                decoded.push((accumulator >> bits) as u8);
                accumulator &= (1u32 << bits).wrapping_sub(1);
            }
        }
        decoded
    }

    fn write_new_entry(path: &Path, raw: &[u8]) {
        let replacement = path.with_extension(format!("replacement-{}", secure_token().unwrap()));
        fs::write(&replacement, raw).unwrap();
        #[cfg(windows)]
        fs::remove_file(path).unwrap();
        fs::rename(replacement, path).unwrap();
    }

    #[test]
    fn fixture_pins_constants_and_canonical_bytes() {
        let fixture = fixture();
        assert_eq!(fixture["protocol"], PROTOCOL);
        let constants = &fixture["constants"];
        assert_eq!(constants["timeout_ms"], TIMEOUT_MS);
        assert_eq!(constants["publication_grace_ms"], PUBLICATION_GRACE_MS);
        assert_eq!(constants["retry_initial_ms"], RETRY_INITIAL_MS);
        assert_eq!(constants["retry_factor_numerator"], RETRY_FACTOR_NUMERATOR);
        assert_eq!(
            constants["retry_factor_denominator"],
            RETRY_FACTOR_DENOMINATOR
        );
        assert_eq!(constants["retry_max_ms"], RETRY_MAX_MS);
        assert_eq!(constants["wait_notice_ms"], WAIT_NOTICE_MS);
        assert_eq!(constants["wait_repeat_ms"], WAIT_REPEAT_MS);
        assert_eq!(constants["max_record_bytes"], MAX_RECORD_BYTES);
        assert_eq!(constants["max_pid"]["posix"], POSIX_MAX_PID);
        assert_eq!(constants["max_pid"]["windows"], u64::from(WINDOWS_MAX_PID));
        let canonical = &fixture["canonical"];
        let raw = render_record(
            canonical["pid"].as_u64().unwrap() as u32,
            canonical["start"].as_str().unwrap(),
            canonical["token"].as_str().unwrap(),
            canonical["label"].as_str().unwrap(),
            canonical["time_ms"].as_u64().unwrap(),
        )
        .unwrap();
        assert_eq!(raw, canonical["record"].as_str().unwrap().as_bytes());
        assert_eq!(raw, decode_base64(canonical["base64"].as_str().unwrap()));
    }

    #[test]
    fn cleanup_liveness_uses_the_lock_observer() {
        assert_eq!(pid_alive_for_cleanup(std::process::id()), Some(true));
        assert_eq!(pid_alive_for_cleanup(0), Some(false));
    }

    #[test]
    fn live_holder_observation_rejects_replacement() {
        let root = temp_dir("live-holder-swap");
        let path = root.join(".index.lock");
        let pid = std::process::id();
        let start = current_process_start_identity().unwrap();
        let first = render_record(pid, &start, &"1".repeat(32), "corpusdb", 1).unwrap();
        fs::write(&path, first).unwrap();
        let observed = observe_live_holder(&path, "corpusdb").unwrap().unwrap();
        let second = render_record(pid, &start, &"2".repeat(32), "corpusdb", 2).unwrap();
        write_new_entry(&path, &second);
        assert!(!live_holder_unchanged(&path, &observed));
        assert!(observe_live_holder(&path, "agrep-rs").unwrap().is_none());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn live_holder_observation_rejects_duplicate_labels() {
        let root = temp_dir("live-holder-duplicate-label");
        let path = root.join(".index.lock");
        let pid = std::process::id();
        let start = current_process_start_identity().unwrap();
        fs::write(
            &path,
            format!(
                "pid={pid} start={start} token={} label=corpusdb label=corpusdb time=0.001\n",
                "1".repeat(32)
            ),
        )
        .unwrap();
        assert!(observe_live_holder(&path, "corpusdb").unwrap().is_none());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn public_wait_notice_is_one_shot_and_recognizes_owned_corpusdb() {
        let mut sent = false;
        assert!(should_emit_public_wait_notice(&mut sent));
        assert!(!should_emit_public_wait_notice(&mut sent));
        assert_eq!(
            public_wait_notice("pid=1 label=corpusdb:0123456789abcdefabcd"),
            "search database update is finishing; waiting..."
        );
        assert_eq!(
            public_timeout_error("pid=1 label=corpusdb:0123456789abcdefabcd"),
            "search database update did not finish; retry, or set AGREP_DEBUG=1 for details"
        );
    }

    #[test]
    fn future_publication_time_is_not_fresh() {
        let now = UNIX_EPOCH + Duration::from_secs(10);
        assert!(!publication_is_fresh(now + Duration::from_secs(1), now));
        assert!(publication_is_fresh(
            now - Duration::from_millis(PUBLICATION_GRACE_MS),
            now
        ));
        assert!(!publication_is_fresh(
            now - Duration::from_millis(PUBLICATION_GRACE_MS + 1),
            now
        ));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn cross_uid_identity_only_classifies_other_users() {
        extern "C" {
            fn geteuid() -> u32;
        }
        let current_uid = unsafe { geteuid() };
        assert_eq!(darwin_cross_uid_identity(current_uid), None);
        let other_uid = current_uid.checked_add(1).unwrap_or(0);
        assert_eq!(
            darwin_cross_uid_identity(other_uid),
            Some(format!("darwin_uid_{other_uid}"))
        );
    }

    #[test]
    fn fixture_legacy_records_keep_the_safety_core() {
        for record in fixture()["legacy_records"].as_array().unwrap() {
            let owner = parse_owner(record.as_str().unwrap().as_bytes()).unwrap();
            assert_eq!(owner.pid, 12345);
        }
    }

    #[test]
    fn fixture_raw_cases_match_platform_classification() {
        for case in fixture()["raw_cases"].as_array().unwrap() {
            let raw = if let Some(encoded) = case.get("base64") {
                decode_base64(encoded.as_str().unwrap())
            } else {
                let repeated = &case["base64_repeat"];
                let byte = decode_base64(repeated["base64_byte"].as_str().unwrap());
                byte.repeat(repeated["count"].as_u64().unwrap() as usize)
            };
            let expected = &case["classification"];
            let platform_expected = if expected.is_object() {
                &expected[if cfg!(windows) { "windows" } else { "posix" }]
            } else {
                expected
            };
            if platform_expected == "owner" {
                let owner = parse_owner(&raw).unwrap();
                assert_eq!(owner.pid, expected["pid"].as_u64().unwrap() as u32);
                assert_eq!(owner.start.as_deref(), expected["start"].as_str());
            } else {
                assert!(
                    parse_owner(&raw).is_none(),
                    "{} unexpectedly parsed",
                    case["name"]
                );
            }
        }
    }

    #[test]
    fn writer_rejects_noncanonical_fields() {
        let token = "0123456789abcdef0123456789abcdef";
        assert!(render_record(0, "proc_1", token, "fixture", 0).is_err());
        assert!(render_record(1, "bad start", token, "fixture", 0).is_err());
        assert!(render_record(1, "bad\u{1}start", token, "fixture", 0).is_err());
        assert!(render_record(1, "proc_1", "ABC", "fixture", 0).is_err());
        assert!(render_record(1, "proc_1", token, "bad label", 0).is_err());
    }

    #[test]
    fn tomb_reclaim_preserves_a_replacement() {
        let dir = temp_dir("replacement");
        let path = dir.join(".index.lock");
        let original = b"pid=12345 start=proc_old\n";
        let replacement = b"pid=54321 start=proc_new\n";
        fs::write(&path, original).unwrap();
        let observed = snapshot(&path).unwrap();
        write_new_entry(&path, replacement);
        assert!(!remove_exact(&path, &observed));
        assert_eq!(fs::read(&path).unwrap(), replacement);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn explicit_release_preserves_an_exact_byte_replacement() {
        let dir = temp_dir("same-bytes");
        let path = dir.join(".index.lock");
        let mut lock = IndexLock::acquire(&path, "fixture", Duration::ZERO).unwrap();
        let raw = fs::read(&path).unwrap();
        write_new_entry(&path, &raw);
        lock.release().unwrap();
        assert_eq!(fs::read(&path).unwrap(), raw);
        fs::remove_file(path).unwrap();
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn tomb_reclaim_removes_the_exact_entry() {
        let dir = temp_dir("exact");
        let path = dir.join(".index.lock");
        fs::write(&path, b"pid=12345 start=proc_old\n").unwrap();
        let observed = snapshot(&path).unwrap();
        assert!(remove_exact(&path, &observed));
        assert!(!path.exists());
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn explicit_release_fails_closed_when_ownership_is_unreadable() {
        use std::os::unix::fs::PermissionsExt;

        let dir = temp_dir("unreadable");
        let path = dir.join(".index.lock");
        let mut lock = IndexLock::acquire(&path, "fixture", Duration::ZERO).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
        let error = lock.release().unwrap_err().to_string();
        assert!(error.contains("could not verify index lock release"));
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        lock.release().unwrap();
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn explicit_release_reports_a_stranded_owned_tomb() {
        let dir = temp_dir("stranded");
        let path = dir.join(".index.lock");
        let mut lock = IndexLock::acquire(&path, "fixture", Duration::ZERO).unwrap();
        let tomb = path.with_file_name(format!(
            "..index.lock.owner-reap-{}-fixture",
            std::process::id()
        ));
        fs::hard_link(&path, &tomb).unwrap();
        fs::remove_file(&path).unwrap();
        let error = lock.release().unwrap_err().to_string();
        assert!(error.contains("could not release owned index lock"));
        fs::remove_file(tomb).unwrap();
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn created_lock_has_exact_owner_only_mode() {
        use std::os::unix::fs::PermissionsExt;

        let dir = temp_dir("mode");
        let path = dir.join(".index.lock");
        let mut lock = IndexLock::acquire(&path, "fixture", Duration::ZERO).unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        lock.release().unwrap();
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn snapshots_reject_symlinks() {
        use std::os::unix::fs::symlink;

        let dir = temp_dir("symlink");
        let target = dir.join("target");
        let path = dir.join(".index.lock");
        fs::write(&target, b"pid=12345\n").unwrap();
        symlink(&target, &path).unwrap();
        assert_eq!(
            snapshot(&path).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn oversized_records_fail_closed() {
        let dir = temp_dir("oversized");
        let path = dir.join(".index.lock");
        let raw = vec![b'x'; MAX_RECORD_BYTES + 1];
        fs::write(&path, &raw).unwrap();
        assert_eq!(
            snapshot(&path).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        let error = IndexLock::acquire(&path, "fixture", Duration::ZERO)
            .err()
            .unwrap()
            .to_string();
        assert_eq!(
            error,
            "another index task did not finish; retry, or set AGREP_DEBUG=1 for details"
        );
        assert_eq!(fs::read(&path).unwrap(), raw);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn own_process_is_live_and_identified() {
        assert_eq!(process_liveness(std::process::id()), Liveness::Alive);
        assert!(process_start_identity(std::process::id()).is_some());
        assert_eq!(process_liveness(0), Liveness::Dead);
    }

    #[test]
    fn dead_owner_record_is_reclaimed_without_touching_a_live_owner() {
        let dir = temp_dir("dead-owner-record");
        let path = dir.join(".indexd.v2.lock");
        let dead = format!(
            "state=derived-adoption pid={MAX_PID} start=fixture writer={} token={}\n",
            "e".repeat(20),
            "a".repeat(32)
        );
        fs::write(&path, &dead).unwrap();
        assert!(reclaim_dead_owner_record(&path).unwrap());
        assert!(!path.exists());

        let start = current_process_start_identity().unwrap();
        let live = format!(
            "state=derived-adoption pid={} start={start} writer={} token={}\n",
            std::process::id(),
            "e".repeat(20),
            "b".repeat(32)
        );
        fs::write(&path, &live).unwrap();
        assert!(!reclaim_dead_owner_record(&path).unwrap());
        assert_eq!(fs::read(&path).unwrap(), live.as_bytes());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn reused_pid_owner_record_is_reclaimed() {
        let dir = temp_dir("reused-owner-record");
        let path = dir.join(".indexd.lock");
        let raw = format!(
            "state=derived-adoption pid={} start=not-this-process writer={} token={}\n",
            std::process::id(),
            "e".repeat(20),
            "c".repeat(32)
        );
        fs::write(&path, raw).unwrap();
        assert!(reclaim_dead_owner_record(&path).unwrap());
        assert!(!path.exists());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn dead_daemon_owner_is_not_reclaimed_as_an_adoption_claim() {
        let dir = temp_dir("dead-daemon-owner");
        let path = dir.join(".indexd.v2.lock");
        let raw = format!(
            "pid={MAX_PID} start=fixture protocol=2 writer={} token={}\n",
            "e".repeat(20),
            "f".repeat(32)
        );
        fs::write(&path, &raw).unwrap();
        assert!(!reclaim_dead_owner_record(&path).unwrap());
        assert_eq!(fs::read(&path).unwrap(), raw.as_bytes());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn cross_uid_sentinel_proves_pid_reuse() {
        assert!(process_identity_reused(
            Some("darwin_100_20"),
            Some("darwin_uid_501")
        ));
        assert!(process_identity_reused(
            Some("darwin_100_20"),
            Some("darwin_101_30")
        ));
    }

    #[test]
    fn reaped_child_is_dead() {
        let mut command = std::process::Command::new(if cfg!(windows) { "cmd" } else { "true" });
        if cfg!(windows) {
            command.args(["/C", "exit"]);
        }
        let mut child = command.spawn().unwrap();
        let pid = child.id();
        child.wait().unwrap();
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(process_liveness(pid), Liveness::Dead);
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn unreaped_zombie_is_dead() {
        extern "C" {
            fn fork() -> i32;
            fn waitpid(pid: i32, status: *mut i32, options: i32) -> i32;
            fn _exit(status: i32) -> !;
        }
        let pid = unsafe { fork() };
        assert!(pid >= 0);
        if pid == 0 {
            unsafe { _exit(0) };
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        while process_liveness(pid as u32) != Liveness::Dead && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(process_liveness(pid as u32), Liveness::Dead);
        let dir = temp_dir("zombie-owner-record");
        let path = dir.join(".indexd.v2.lock");
        fs::write(
            &path,
            format!(
                "state=derived-adoption pid={pid} start=unknown writer={} token={}\n",
                "e".repeat(20),
                "d".repeat(32)
            ),
        )
        .unwrap();
        assert!(reclaim_dead_owner_record(&path).unwrap());
        let mut status = 0;
        assert_eq!(unsafe { waitpid(pid, &mut status, 0) }, pid);
        fs::remove_dir_all(dir).unwrap();
    }
}
