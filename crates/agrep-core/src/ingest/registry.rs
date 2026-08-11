//! The one registration point for adapters. Adding an agent is a trait impl in its module
//! plus a single line in `ADAPTERS` here.
//!
//! The driver runs cache-driven adapters sequentially sharing the per-file parse cache; the
//! full-parse (Always) adapters run in a sibling rayon task; results are concatenated and
//! deduped by message content/position or event call id. Sessions with colliding source turns
//! receive deterministic unique turn numbers before publication.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{ErrorKind, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::sync::{OnceLock, RwLock};

use anyhow::Context as _;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::ingest_cache::IngestCache;
use crate::model::{Event, Message};

/// How an adapter decides a source is unchanged since the last index. Defined here so each
/// adapter states its strategy; the driver only uses it to route to the matching parse path
/// (Stat/Token -> the per-file cache; Always -> full reparse).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Fingerprint {
    /// Reparse when a file's (mtime, filesystem-change-id, size) moves
    /// (claude/codex/opencode/gemini).
    Stat,
    /// Reparse when a per-conversation token changes (updatedAt / value hash) - one sqlite
    /// file, many conversations, so staleness is per-session (crush, cursor).
    Token,
    /// Always full-parse; no per-file cache (the small stores: antigravity/kimi/cline).
    Always,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TokenAvailability {
    Data(Vec<(String, String)>),
    Empty,
    Unreadable(Vec<TokenReadIssue>),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TokenReadIssue {
    pub path: PathBuf,
    pub reason: String,
}

impl TokenReadIssue {
    pub fn new(path: impl Into<PathBuf>, reason: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            reason: reason.into(),
        }
    }
}

impl TokenAvailability {
    pub fn from_tokens(tokens: Vec<(String, String)>) -> Self {
        if tokens.is_empty() {
            Self::Empty
        } else {
            Self::Data(tokens)
        }
    }
}

/// A `Fingerprint::Token` conversation's staleness token: the store's own
/// `updatedAt` when it records one, else an FNV-1a hash over the conversation's serialized
/// JSON value. Count plus last-id is invariant under an in-place edit or a
/// delete-one-add-one, which strands the conversation as permanently "unchanged" (silent
/// staleness); `--full` is the documented recovery. crush keys its per-conversation cache
/// on this; cursor hashes raw row bytes via `fnv_token` directly.
pub fn token_fingerprint(value: &serde_json::Value) -> String {
    if let Some(u) = value.get("updatedAt") {
        match u {
            serde_json::Value::String(s) if !s.is_empty() => return format!("u:{s}"),
            serde_json::Value::Number(n) => return format!("u:{n}"),
            _ => {}
        }
    }
    fnv_token(value.to_string().as_bytes())
}

/// The FNV-1a half of `token_fingerprint`, for adapters whose store records no usable
/// updatedAt at all (cursor hashes raw row bytes directly instead of re-serializing JSON).
pub fn fnv_token(bytes: &[u8]) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("h:{h:016x}")
}

/// One agent's transcript adapter: discover its store under `home()`, parse sessions, and
/// emit the user's messages + tool/subagent events. `collect` takes the shared per-file parse
/// cache; full-parse adapters ignore it. Impls live in each adapter module (byte-identical
/// wrappers over the existing `collect` free fns).
pub trait Adapter: Sync {
    fn name(&self) -> &'static str;
    fn fingerprint(&self) -> Fingerprint;
    fn collect(&self, cache: &mut IngestCache) -> (Vec<Message>, Vec<Event>);
    fn collect_checked(
        &self,
        cache: &mut IngestCache,
    ) -> (
        Vec<Message>,
        Vec<Event>,
        crate::ingest_cache::ReadOutcome,
        Vec<crate::ingest_cache::SourceReadIssue>,
    ) {
        let (messages, events) = self.collect(cache);
        (
            messages,
            events,
            crate::ingest_cache::ReadOutcome::Complete,
            Vec::new(),
        )
    }
    /// Where this adapter's store lives (dirs or files; absent candidates are fine).
    /// The doctor freshness canary stats these: store activity the parser never turns
    /// into messages is adapter drift, and this is what makes it visible.
    fn store_roots(&self) -> Vec<std::path::PathBuf>;
    /// Is this file conversation CONTENT the adapter would parse? The canary counts
    /// only these - state/config files update on app boots without any new sessions,
    /// and a canary that fires on those trains everyone to ignore it.
    fn store_content(&self, _path: &std::path::Path) -> bool {
        true
    }
    /// Inputs whose unchanged state permits skipping this adapter altogether. Usually these
    /// are the same as the doctor/audit roots, but an adapter may add attribution metadata
    /// which affects emitted rows without itself being conversation content.
    fn freshness_roots(&self) -> Vec<PathBuf> {
        self.store_roots()
    }
    fn freshness_content(&self, path: &Path) -> bool {
        self.store_content(path)
    }
    /// Exact per-conversation tokens for `Fingerprint::Token` stores.
    fn freshness_tokens(&self) -> TokenAvailability {
        TokenAvailability::Unreadable(vec![TokenReadIssue::new(
            self.runtime_issue_root(),
            "conversation token snapshot unavailable",
        )])
    }
    /// Exact live tokens keyed the same way as `intake_stats.json` entries.
    fn intake_tokens(&self) -> TokenAvailability {
        self.freshness_tokens()
    }
    /// Actionable adapter root used only when a guarded collector cannot name a finer path.
    fn runtime_issue_root(&self) -> PathBuf {
        self.freshness_roots()
            .into_iter()
            .next()
            .unwrap_or_else(crate::ingest::home)
    }
}

/// Every registered adapter. This slice is the single source of truth the driver, the CLI
/// help, and the event delete-sweep all read - add an adapter here and it is wired everywhere.
pub static ADAPTERS: &[&dyn Adapter] = &[
    &crate::ingest::claude::Claude,
    &crate::ingest::codex::Codex,
    &crate::ingest::opencode::Opencode,
    &crate::ingest::antigravity::Antigravity,
    &crate::ingest::kimi::Kimi,
    &crate::ingest::cline::Cline,
    &crate::ingest::gemini::Gemini,
    &crate::ingest::crush::Crush,
    &crate::ingest::cursor::Cursor,
    &crate::ingest::pi::Pi,
];

#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
pub enum ChangeToken {
    Metadata(u64),
    ContentSha256([u8; 32]),
}

impl ChangeToken {
    pub fn append_key(&self, output: &mut Vec<u8>) {
        match self {
            Self::Metadata(value) => {
                output.push(0);
                output.extend_from_slice(&value.to_le_bytes());
            }
            Self::ContentSha256(value) => {
                output.push(1);
                output.extend_from_slice(value);
            }
        }
    }
}

/// Versioned, exact snapshot of the source state observed before a successful ingest. A match
/// lets the CLI return before loading/materializing the parse cache. `CACHE_VERSION` is part of
/// the payload: a parser/schema bump can never be hidden by an old source snapshot.
const SOURCE_SNAPSHOT_VERSION: u32 = 11;
const SOURCE_AUDIT_IDENTITY_DOMAIN: &[u8] = b"agrep-source-audit-identity-v1\0";

#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct SourceIssue {
    agent: String,
    path: String,
    kind: String,
    reason: String,
}

impl SourceIssue {
    fn from_io(agent: &str, path: &Path, error: &std::io::Error) -> Self {
        let kind = match error.kind() {
            ErrorKind::NotFound => "not-found",
            ErrorKind::PermissionDenied => "permission-denied",
            ErrorKind::Interrupted => "interrupted",
            ErrorKind::InvalidData => "invalid-data",
            ErrorKind::UnexpectedEof => "unexpected-eof",
            _ => "io-error",
        };
        Self::new(agent, path, kind, error)
    }

    fn from_anyhow(agent: &str, path: &Path, error: &anyhow::Error) -> Self {
        if let Some(cause) = error.root_cause().downcast_ref::<std::io::Error>() {
            return Self::from_io(agent, path, cause);
        }
        if error
            .root_cause()
            .downcast_ref::<std::time::SystemTimeError>()
            .is_some()
        {
            // A pre-epoch mtime cannot heal on retry; name it as its own durable fact.
            return Self::new(agent, path, "invalid-mtime", error);
        }
        Self::new(agent, path, "io-error", error)
    }

    fn new(agent: &str, path: &Path, kind: &str, reason: impl std::fmt::Display) -> Self {
        Self {
            agent: agent.to_string(),
            path: path.to_string_lossy().into_owned(),
            kind: kind.to_string(),
            reason: reason.to_string(),
        }
    }

    pub fn agent(&self) -> &str {
        &self.agent
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    pub fn kind(&self) -> &str {
        &self.kind
    }

    pub fn reason(&self) -> &str {
        &self.reason
    }
}

#[derive(Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
struct SourceFile {
    agent: String,
    #[cfg_attr(windows, serde(with = "windows_path_serde"))]
    path: PathBuf,
    len: u64,
    mtime_secs: u64,
    mtime_nanos: u32,
    /// Identity independent of the user-restorable mtime. Unix uses inode change-time;
    /// Windows uses the file USN, with a SHA-256 fallback off journaled volumes.
    change_token: ChangeToken,
    #[cfg(windows)]
    file_identity: Option<WindowsFileIdentity>,
    /// Always-parsed stores promise stronger freshness than stat keys, so their small source
    /// files are byte-hashed. Stat adapters use filesystem change identity; Token adapters add
    /// their exact conversation tokens below.
    content_hash: Option<u64>,
}

#[cfg(windows)]
mod windows_path_serde {
    use std::ffi::OsString;
    use std::os::windows::ffi::{OsStrExt, OsStringExt};
    use std::path::{Path, PathBuf};

    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S>(path: &Path, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        path.as_os_str()
            .encode_wide()
            .collect::<Vec<_>>()
            .serialize(serializer)
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<PathBuf, D::Error>
    where
        D: Deserializer<'de>,
    {
        let units = Vec::<u16>::deserialize(deserializer)?;
        Ok(PathBuf::from(OsString::from_wide(&units)))
    }
}

#[cfg(unix)]
pub fn metadata_change_token(
    _path: &Path,
    metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    use std::os::unix::fs::MetadataExt;
    let secs = metadata.ctime() as u64;
    Ok(ChangeToken::Metadata(
        secs.rotate_left(17) ^ metadata.ctime_nsec() as u64,
    ))
}

#[cfg(windows)]
#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct WindowsFileIdentity {
    pub(crate) kind: u8,
    pub(crate) volume: u64,
    pub(crate) id: [u8; 16],
}

#[cfg(windows)]
impl WindowsFileIdentity {
    pub fn cache_key(self) -> String {
        use std::fmt::Write as _;

        let mut key = String::with_capacity(56);
        key.push_str("\0file\0");
        write!(&mut key, "{}:{:016x}:", self.kind, self.volume).unwrap();
        for byte in self.id {
            write!(&mut key, "{byte:02x}").unwrap();
        }
        key
    }
}

#[cfg(windows)]
pub fn metadata_change_token(path: &Path, metadata: &fs::Metadata) -> std::io::Result<ChangeToken> {
    windows_metadata_identity(path, metadata, false, false, false).map(|(token, _)| token)
}

#[cfg(windows)]
pub fn metadata_change_token_with_file_identity(
    path: &Path,
    metadata: &fs::Metadata,
) -> std::io::Result<(ChangeToken, WindowsFileIdentity)> {
    let (token, identity) = windows_metadata_identity(path, metadata, true, false, false)?;
    let identity = identity.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "filesystem did not provide a stable file identity",
        )
    })?;
    Ok((token, identity))
}

#[cfg(windows)]
fn audit_metadata_change_token_with_file_identity(
    path: &Path,
    metadata: &fs::Metadata,
) -> std::io::Result<(ChangeToken, WindowsFileIdentity)> {
    let (token, identity) = windows_metadata_identity(path, metadata, true, false, true)?;
    let identity = identity.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "filesystem did not provide a stable file identity",
        )
    })?;
    Ok((token, identity))
}

#[cfg(windows)]
pub fn content_sha256_with_file_identity(
    path: &Path,
    metadata: &fs::Metadata,
) -> std::io::Result<([u8; 32], WindowsFileIdentity)> {
    let (token, identity) = windows_metadata_identity(path, metadata, true, true, false)?;
    let ChangeToken::ContentSha256(digest) = token else {
        unreachable!("forced content hashing returned metadata identity")
    };
    let identity = identity.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "filesystem did not provide a stable file identity",
        )
    })?;
    Ok((digest, identity))
}

#[cfg(windows)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WindowsHandleState {
    identity: WindowsFileIdentity,
    legacy_volume: u64,
    legacy_inode: u64,
    size: u64,
    last_write: u64,
    change_time: u64,
    attributes: u32,
}

#[cfg(any(windows, test))]
fn windows_file_size_from_parts(high: u32, low: u32) -> std::io::Result<u64> {
    let size = (u64::from(high) << 32) | u64::from(low);
    if size > i64::MAX as u64 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid negative source size",
        ));
    }
    Ok(size)
}

#[cfg(windows)]
fn windows_audit_change_token(state: WindowsHandleState) -> ChangeToken {
    ChangeToken::Metadata(state.change_time)
}

/// Handle identity without extent: what stays true of a file a writer is still appending to.
/// Size and timestamps are deliberately excluded - a source that moves between two observations
/// is changing, not unreadable, and the token itself is what reports the change.
#[cfg(windows)]
fn same_open_file(left: &WindowsHandleState, right: &WindowsHandleState) -> bool {
    left.identity == right.identity
        && left.legacy_volume == right.legacy_volume
        && left.legacy_inode == right.legacy_inode
        && left.attributes == right.attributes
}

#[cfg(windows)]
fn windows_handle_state(
    handle: std::os::windows::io::RawHandle,
) -> std::io::Result<WindowsHandleState> {
    use windows_sys::Win32::Storage::FileSystem::{
        FileBasicInfo, FileIdInfo, GetFileInformationByHandle, GetFileInformationByHandleEx,
        BY_HANDLE_FILE_INFORMATION, FILE_BASIC_INFO, FILE_ID_INFO,
    };

    let mut legacy = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(handle, &mut legacy) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let legacy_volume = u64::from(legacy.dwVolumeSerialNumber);
    let legacy_inode = (u64::from(legacy.nFileIndexHigh) << 32) | u64::from(legacy.nFileIndexLow);
    let mut file_id = FILE_ID_INFO::default();
    let identity = if unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileIdInfo,
            (&mut file_id as *mut FILE_ID_INFO).cast(),
            std::mem::size_of::<FILE_ID_INFO>() as u32,
        )
    } != 0
    {
        WindowsFileIdentity {
            kind: 1,
            volume: file_id.VolumeSerialNumber,
            id: file_id.FileId.Identifier,
        }
    } else {
        let mut id = [0_u8; 16];
        id[..4].copy_from_slice(&legacy.nFileIndexLow.to_le_bytes());
        id[4..8].copy_from_slice(&legacy.nFileIndexHigh.to_le_bytes());
        WindowsFileIdentity {
            kind: 0,
            volume: u64::from(legacy.dwVolumeSerialNumber),
            id,
        }
    };
    let mut basic = FILE_BASIC_INFO::default();
    if unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileBasicInfo,
            (&mut basic as *mut FILE_BASIC_INFO).cast(),
            std::mem::size_of::<FILE_BASIC_INFO>() as u32,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    let size = windows_file_size_from_parts(legacy.nFileSizeHigh, legacy.nFileSizeLow)?;
    Ok(WindowsHandleState {
        identity,
        legacy_volume,
        legacy_inode,
        size,
        last_write: basic.LastWriteTime as u64,
        change_time: basic.ChangeTime as u64,
        attributes: basic.FileAttributes,
    })
}

#[cfg(windows)]
fn windows_open_file_change_token(
    file: &fs::File,
    metadata: &fs::Metadata,
    force_content_hash: bool,
    audit_change_time_fallback: bool,
) -> std::io::Result<(ChangeToken, WindowsHandleState)> {
    use std::io::Read;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Security::Cryptography::{
        CryptAcquireContextW, CryptCreateHash, CryptDestroyHash, CryptGetHashParam, CryptHashData,
        CryptReleaseContext, CALG_SHA_256, CRYPT_VERIFYCONTEXT, HP_HASHVAL, PROV_RSA_AES,
    };
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
    use windows_sys::Win32::System::Ioctl::{FSCTL_READ_FILE_USN_DATA, READ_FILE_USN_DATA};
    use windows_sys::Win32::System::IO::DeviceIoControl;

    let initial_identity = windows_handle_state(file.as_raw_handle())?;
    if initial_identity.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "source changed into a reparse point while snapshotting",
        ));
    }
    // The caller's stat happened before this open, so the handle may already show a later
    // extent. That skew is one-directional and safe: the recorded len/mtime is never newer
    // than the token, so a mismatch can only force an extra reparse, never hide a change.
    if metadata_is_link(metadata) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "source is a link, not a plain regular file",
        ));
    }
    if !force_content_hash {
        let request = READ_FILE_USN_DATA {
            MinMajorVersion: 2,
            MaxMajorVersion: 3,
        };
        let mut output = [0_u8; 1024];
        let mut returned = 0_u32;
        let ok = unsafe {
            DeviceIoControl(
                file.as_raw_handle(),
                FSCTL_READ_FILE_USN_DATA,
                (&request as *const READ_FILE_USN_DATA).cast(),
                std::mem::size_of::<READ_FILE_USN_DATA>() as u32,
                output.as_mut_ptr().cast(),
                output.len() as u32,
                &mut returned,
                std::ptr::null_mut(),
            )
        };
        if ok != 0 {
            let bytes = returned as usize;
            if bytes < 8 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "invalid file USN response",
                ));
            }
            let record_len = u32::from_le_bytes(output[0..4].try_into().unwrap()) as usize;
            let major = u16::from_le_bytes(output[4..6].try_into().unwrap());
            let usn_offset = match major {
                2 => 24,
                3 => 40,
                _ => usize::MAX,
            };
            if usn_offset == usize::MAX || record_len > bytes || record_len < usn_offset + 8 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "invalid file USN response",
                ));
            }
            let usn = i64::from_le_bytes(output[usn_offset..usn_offset + 8].try_into().unwrap());
            if !same_open_file(
                &windows_handle_state(file.as_raw_handle())?,
                &initial_identity,
            ) {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "source was replaced while reading its freshness identity",
                ));
            }
            return Ok((
                ChangeToken::Metadata((usn as u64).rotate_left(7) ^ 0x5553_4e5f_4649_4c45),
                initial_identity,
            ));
        }
    }

    if audit_change_time_fallback {
        // Use identity-only comparison: a file whose extent moved between the stat and this
        // re-read is changing, not unreadable, and change_time already records the move.
        let post = windows_handle_state(file.as_raw_handle())?;
        if post.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
            || !same_open_file(&post, &initial_identity)
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "source changed while reading its audit identity",
            ));
        }
        return Ok((
            windows_audit_change_token(initial_identity),
            initial_identity,
        ));
    }

    let mut provider = 0_usize;
    let mut hash = 0_usize;
    let result = (|| {
        if unsafe {
            CryptAcquireContextW(
                &mut provider,
                std::ptr::null(),
                std::ptr::null(),
                PROV_RSA_AES,
                CRYPT_VERIFYCONTEXT,
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error());
        }
        if unsafe { CryptCreateHash(provider, CALG_SHA_256, 0, 0, &mut hash) } == 0 {
            return Err(std::io::Error::last_os_error());
        }
        let mut reader = file;
        reader.seek(SeekFrom::Start(0))?;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = reader.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            if unsafe { CryptHashData(hash, buffer.as_ptr(), count as u32, 0) } == 0 {
                return Err(std::io::Error::last_os_error());
            }
        }
        let mut digest = [0_u8; 32];
        let mut digest_len = digest.len() as u32;
        if unsafe { CryptGetHashParam(hash, HP_HASHVAL, digest.as_mut_ptr(), &mut digest_len, 0) }
            == 0
        {
            return Err(std::io::Error::last_os_error());
        }
        if digest_len != digest.len() as u32 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "invalid SHA-256 provider response",
            ));
        }
        Ok(digest)
    })();
    if hash != 0 {
        unsafe { CryptDestroyHash(hash) };
    }
    if provider != 0 {
        unsafe { CryptReleaseContext(provider, 0) };
    }
    let digest = result?;
    // Use identity-only comparison: the hash covers whatever was read; extent movement
    // produces a correct (different) hash, not a corrupt one.
    let post = windows_handle_state(file.as_raw_handle())?;
    if post.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || !same_open_file(&post, &initial_identity)
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "source changed while hashing its freshness identity",
        ));
    }
    Ok((ChangeToken::ContentSha256(digest), initial_identity))
}

#[cfg(windows)]
fn windows_metadata_identity(
    path: &Path,
    metadata: &fs::Metadata,
    need_file_id: bool,
    force_content_hash: bool,
    audit_change_time_fallback: bool,
) -> std::io::Result<(ChangeToken, Option<WindowsFileIdentity>)> {
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE,
        FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    let file = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)?;
    let (token, initial_identity) = windows_open_file_change_token(
        &file,
        metadata,
        force_content_hash,
        audit_change_time_fallback,
    )?;
    let path_after = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)?;
    let after = windows_handle_state(path_after.as_raw_handle())?;
    // The TOCTOU guard this re-open exists for: the path must still resolve to the file the
    // token describes. Its extent may have moved in between; a live writer is not a swap.
    if after.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || !same_open_file(&after, &initial_identity)
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "source was replaced while reading its freshness identity",
        ));
    }
    Ok((token, need_file_id.then_some(initial_identity.identity)))
}

#[cfg(not(any(unix, windows)))]
pub fn metadata_change_token(
    _path: &Path,
    _metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    Ok(ChangeToken::Metadata(0))
}

pub fn metadata_is_link(metadata: &fs::Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
        metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    false
}

#[cfg(target_os = "linux")]
fn secure_read_options(options: &mut fs::OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;

    options.custom_flags(0x20_000 | 0x800);
}

#[cfg(target_os = "macos")]
fn secure_read_options(options: &mut fs::OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;

    options.custom_flags(0x100 | 0x4);
}

#[cfg(windows)]
fn secure_read_options(options: &mut fs::OpenOptions) {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    options
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn secure_read_options(_options: &mut fs::OpenOptions) {}

#[cfg(unix)]
fn same_plain_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(windows)]
fn same_plain_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    left.creation_time() == right.creation_time()
        && left.file_size() == right.file_size()
        && left.file_attributes() == right.file_attributes()
}

#[cfg(not(any(unix, windows)))]
fn same_plain_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.len() == right.len() && left.modified().ok() == right.modified().ok()
}

#[cfg(unix)]
fn same_plain_snapshot(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    same_plain_file(left, right)
        && left.len() == right.len()
        && left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime() == right.ctime()
        && left.ctime_nsec() == right.ctime_nsec()
}

#[cfg(not(unix))]
fn same_plain_snapshot(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    same_plain_file(left, right)
        && left.len() == right.len()
        && left.modified().ok() == right.modified().ok()
}

fn open_regular_snapshot(path: &Path, before: &fs::Metadata) -> std::io::Result<fs::File> {
    if metadata_is_link(before) || !before.is_file() {
        return Err(std::io::Error::new(
            ErrorKind::InvalidInput,
            "source is not a plain regular file",
        ));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let file = options.open(path)?;
    let opened = file.metadata()?;
    let after = fs::symlink_metadata(path)?;
    if metadata_is_link(&opened)
        || !opened.is_file()
        || !same_plain_snapshot(before, &opened)
        || !same_plain_snapshot(&opened, &after)
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while opening",
        ));
    }
    Ok(file)
}

pub(crate) fn with_regular_file_snapshot<T>(
    path: &Path,
    visitor: impl FnOnce(&mut fs::File) -> std::io::Result<T>,
) -> std::io::Result<Option<T>> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut file = open_regular_snapshot(path, &before)?;
    let value = visitor(&mut file)?;
    verify_regular_snapshot(path, &file, &before)?;
    Ok(Some(value))
}

#[cfg(not(windows))]
fn verify_regular_snapshot(
    path: &Path,
    file: &fs::File,
    before: &fs::Metadata,
) -> std::io::Result<()> {
    let opened = file.metadata()?;
    let after = fs::symlink_metadata(path)?;
    if !same_plain_snapshot(before, &opened) || !same_plain_snapshot(&opened, &after) {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn verify_regular_snapshot(
    path: &Path,
    file: &fs::File,
    before: &fs::Metadata,
) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;

    let opened = file.metadata()?;
    if !same_plain_snapshot(before, &opened) {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let path_after = options.open(path)?;
    let opened_state = windows_handle_state(file.as_raw_handle())?;
    let path_state = windows_handle_state(path_after.as_raw_handle())?;
    if opened_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || path_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || opened_state != path_state
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

/// Identity without extent: what stays true of a file a writer is still appending to.
#[cfg(unix)]
fn same_growing_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    same_plain_file(left, right)
}

#[cfg(windows)]
fn same_growing_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    left.creation_time() == right.creation_time()
        && left.file_attributes() == right.file_attributes()
}

#[cfg(not(any(unix, windows)))]
fn same_growing_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.len() <= right.len()
}

#[cfg(not(windows))]
fn verify_growing_snapshot(path: &Path, file: &fs::File, read: u64) -> std::io::Result<()> {
    let opened = file.metadata()?;
    let after = fs::symlink_metadata(path)?;
    if !opened.is_file()
        || !after.is_file()
        || metadata_is_link(&after)
        || !same_growing_file(&opened, &after)
        || opened.len() < read
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn verify_growing_snapshot(path: &Path, file: &fs::File, read: u64) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;

    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let path_after = options.open(path)?;
    let opened_state = windows_handle_state(file.as_raw_handle())?;
    let path_state = windows_handle_state(path_after.as_raw_handle())?;
    if opened_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || path_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || opened_state.identity != path_state.identity
        || opened_state.attributes != path_state.attributes
        || opened_state.size < read
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

/// Read the bytes a source held when it was opened, tolerating appends past them.
///
/// An agent transcript is an append-only log that grows under every reader, so the strict
/// snapshot readers below - which require len, mtime and ctime to be untouched - refuse the
/// busiest session on the box on every pass, forever. The leading `len` bytes are a real
/// generation of the file; the appended tail is the next pass's work. Replacement and
/// truncation still fail. A truncate that regrows past `len` inside one read is invisible to
/// metadata and is corrected on the next pass, whose stamp differs.
fn read_growing_regular_file_from_meta(
    path: &Path,
    max_bytes: u64,
    before: fs::Metadata,
) -> std::io::Result<Vec<u8>> {
    read_growing_regular_file_from_meta_with(path, max_bytes, before, || {})
}

fn read_growing_regular_file_from_meta_with(
    path: &Path,
    max_bytes: u64,
    before: fs::Metadata,
    after_read: impl FnOnce(),
) -> std::io::Result<Vec<u8>> {
    if metadata_is_link(&before) || !before.is_file() {
        return Err(std::io::Error::new(
            ErrorKind::InvalidInput,
            "source is not a plain regular file",
        ));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let mut file = options.open(path)?;
    let opened = file.metadata()?;
    if metadata_is_link(&opened) || !opened.is_file() || !same_growing_file(&before, &opened) {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while opening",
        ));
    }
    let len = opened.len();
    if len > max_bytes {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            format!("source exceeds {max_bytes} bytes"),
        ));
    }
    let mut body = Vec::with_capacity(len as usize);
    file.by_ref().take(len).read_to_end(&mut body)?;
    if body.len() as u64 != len {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source shrank while reading",
        ));
    }
    after_read();
    verify_growing_snapshot(path, &file, len)?;
    Ok(body)
}

pub fn read_growing_regular_file(path: &Path, max_bytes: u64) -> std::io::Result<Option<Vec<u8>>> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    read_growing_regular_file_from_meta(path, max_bytes, before).map(Some)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BoundedRegularFileSnapshot {
    pub bytes: Vec<u8>,
    pub modified: std::time::SystemTime,
}

fn read_bounded_regular_file_snapshot_from_meta(
    path: &Path,
    max_bytes: u64,
    before: fs::Metadata,
) -> std::io::Result<BoundedRegularFileSnapshot> {
    read_bounded_regular_file_snapshot_from_meta_with(path, max_bytes, before, || {})
}

fn read_bounded_regular_file_snapshot_from_meta_with(
    path: &Path,
    max_bytes: u64,
    before: fs::Metadata,
    after_read: impl FnOnce(),
) -> std::io::Result<BoundedRegularFileSnapshot> {
    if before.len() > max_bytes {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            format!("source exceeds {max_bytes} bytes"),
        ));
    }
    let mut file = open_regular_snapshot(path, &before)?;
    let opened = file.metadata()?;
    let mut body = Vec::with_capacity(opened.len() as usize);
    file.by_ref()
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut body)?;
    if body.len() as u64 != before.len() || body.len() as u64 > max_bytes {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source length changed while reading",
        ));
    }
    after_read();
    verify_regular_snapshot(path, &file, &before)?;
    Ok(BoundedRegularFileSnapshot {
        bytes: body,
        modified: opened.modified()?,
    })
}

fn read_bounded_regular_file_from_meta(
    path: &Path,
    max_bytes: u64,
    before: fs::Metadata,
) -> std::io::Result<Vec<u8>> {
    read_bounded_regular_file_snapshot_from_meta(path, max_bytes, before)
        .map(|snapshot| snapshot.bytes)
}

pub fn read_bounded_regular_file(path: &Path, max_bytes: u64) -> std::io::Result<Option<Vec<u8>>> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    read_bounded_regular_file_from_meta(path, max_bytes, before).map(Some)
}

pub fn read_bounded_regular_file_snapshot(
    path: &Path,
    max_bytes: u64,
) -> std::io::Result<Option<BoundedRegularFileSnapshot>> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    read_bounded_regular_file_snapshot_from_meta(path, max_bytes, before).map(Some)
}

/// Copy one exact plain-file generation to a caller-owned private path.
///
/// The source is opened no-follow, verified against its path before and after
/// the streaming copy, and never hard-linked. This is intentionally separate
/// from ordinary reads: SQLite ownership probes use it to inspect a private
/// main/journal/WAL family without exposing live WAL or SHM bytes to SQLite.
pub fn copy_regular_file_snapshot(path: &Path, target: &Path) -> std::io::Result<bool> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    let mut source = open_regular_snapshot(path, &before)?;
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut output = options.open(target)?;
    let copied = std::io::copy(&mut source, &mut output)?;
    if copied != before.len() {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source length changed while copying",
        ));
    }
    output.sync_all()?;
    verify_regular_snapshot(path, &source, &before)?;
    if output.metadata()?.len() != copied {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "private snapshot length does not match its source",
        ));
    }
    Ok(true)
}

pub fn regular_file_equals(path: &Path, expected: &[u8]) -> std::io::Result<bool> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    if metadata_is_link(&before) || !before.is_file() || before.len() != expected.len() as u64 {
        return Ok(false);
    }
    read_bounded_regular_file_from_meta(path, expected.len() as u64, before)
        .map(|body| body == expected)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RegularFileEdgeSnapshot {
    pub len: u64,
    pub modified: std::time::SystemTime,
    pub change_token: ChangeToken,
    pub head: Vec<u8>,
    pub tail: Vec<u8>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RegularFileReaderIdentity {
    pub len: u64,
    pub modified_ns: u64,
    pub changed_ns: u64,
    pub device: u64,
    pub inode: u64,
}

#[cfg(unix)]
fn regular_file_reader_identity_from_file(
    file: &fs::File,
) -> std::io::Result<RegularFileReaderIdentity> {
    use std::os::unix::fs::MetadataExt;

    let metadata = file.metadata()?;
    let to_ns = |seconds: i64, nanos: i64| {
        u64::try_from(i128::from(seconds) * 1_000_000_000 + i128::from(nanos)).map_err(|_| {
            std::io::Error::new(
                ErrorKind::InvalidData,
                "file timestamp is outside proof range",
            )
        })
    };
    Ok(RegularFileReaderIdentity {
        len: metadata.len(),
        modified_ns: to_ns(metadata.mtime(), metadata.mtime_nsec())?,
        changed_ns: to_ns(metadata.ctime(), metadata.ctime_nsec())?,
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

#[cfg(windows)]
fn windows_reader_identity(
    state: WindowsHandleState,
) -> std::io::Result<RegularFileReaderIdentity> {
    const WINDOWS_EPOCH_TICKS: u64 = 116_444_736_000_000_000;
    let modified_ns = state
        .last_write
        .checked_sub(WINDOWS_EPOCH_TICKS)
        .and_then(|ticks| ticks.checked_mul(100))
        .ok_or_else(|| {
            std::io::Error::new(ErrorKind::InvalidData, "file mtime is outside proof range")
        })?;
    let changed_ns = state.change_time.checked_mul(100).ok_or_else(|| {
        std::io::Error::new(
            ErrorKind::InvalidData,
            "file change time is outside proof range",
        )
    })?;
    Ok(RegularFileReaderIdentity {
        len: state.size,
        modified_ns,
        changed_ns,
        device: state.legacy_volume,
        inode: state.legacy_inode,
    })
}

#[cfg(windows)]
fn regular_file_reader_identity_from_file(
    file: &fs::File,
) -> std::io::Result<RegularFileReaderIdentity> {
    use std::os::windows::io::AsRawHandle;

    windows_reader_identity(windows_handle_state(file.as_raw_handle())?)
}

#[cfg(not(any(unix, windows)))]
fn regular_file_reader_identity_from_file(
    file: &fs::File,
) -> std::io::Result<RegularFileReaderIdentity> {
    let metadata = file.metadata()?;
    let modified_ns = metadata
        .modified()?
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| std::io::Error::new(ErrorKind::InvalidData, "file mtime predates epoch"))?
        .as_nanos()
        .try_into()
        .map_err(|_| std::io::Error::new(ErrorKind::InvalidData, "file mtime is too large"))?;
    Ok(RegularFileReaderIdentity {
        len: metadata.len(),
        modified_ns,
        changed_ns: 0,
        device: 0,
        inode: 0,
    })
}

pub fn regular_file_reader_identity(
    path: &Path,
) -> std::io::Result<Option<RegularFileReaderIdentity>> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let file = open_regular_snapshot(path, &before)?;
    let identity = regular_file_reader_identity_from_file(&file)?;
    verify_regular_snapshot(path, &file, &before)?;
    Ok(Some(identity))
}

#[cfg(windows)]
fn open_file_change_token(
    _path: &Path,
    file: &fs::File,
    metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    windows_open_file_change_token(file, metadata, false, false).map(|(token, _)| token)
}

#[cfg(not(windows))]
fn open_file_change_token(
    path: &Path,
    _file: &fs::File,
    metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    metadata_change_token(path, metadata)
}

fn regular_file_edge_snapshot_from_meta(
    path: &Path,
    edge_bytes: usize,
    before: fs::Metadata,
) -> std::io::Result<RegularFileEdgeSnapshot> {
    let mut file = open_regular_snapshot(path, &before)?;
    let opened = file.metadata()?;
    let len = opened.len();
    let head_len = len.min(edge_bytes as u64) as usize;
    let mut head = vec![0; head_len];
    file.read_exact(&mut head)?;
    let mut tail = Vec::new();
    if len > edge_bytes as u64 && edge_bytes != 0 {
        let tail_len = len.min(edge_bytes as u64) as usize;
        let offset = i64::try_from(tail_len).map_err(|_| {
            std::io::Error::new(ErrorKind::InvalidInput, "edge size exceeds seek range")
        })?;
        file.seek(SeekFrom::End(-offset))?;
        tail.resize(tail_len, 0);
        file.read_exact(&mut tail)?;
    }
    let change_token = open_file_change_token(path, &file, &opened)?;
    verify_regular_snapshot(path, &file, &before)?;
    Ok(RegularFileEdgeSnapshot {
        len,
        modified: opened.modified()?,
        change_token,
        head,
        tail,
    })
}

pub fn regular_file_edge_snapshot(
    path: &Path,
    edge_bytes: usize,
) -> std::io::Result<Option<RegularFileEdgeSnapshot>> {
    let before = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    regular_file_edge_snapshot_from_meta(path, edge_bytes, before).map(Some)
}

fn hash_regular_file_from_meta(path: &Path, before: &fs::Metadata) -> std::io::Result<u64> {
    let mut file = open_regular_snapshot(path, before)?;
    let mut remaining = before.len();
    let mut hash = 0xcbf29ce484222325_u64;
    let mut chunk = [0_u8; 64 * 1024];
    while remaining != 0 {
        let wanted = remaining.min(chunk.len() as u64) as usize;
        let count = file.read(&mut chunk[..wanted])?;
        if count == 0 {
            return Err(std::io::Error::new(
                ErrorKind::UnexpectedEof,
                "source shrank while hashing",
            ));
        }
        for byte in &chunk[..count] {
            hash ^= *byte as u64;
            hash = hash.wrapping_mul(0x100000001b3);
        }
        remaining -= count as u64;
    }
    if file.read(&mut chunk[..1])? != 0 {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source grew while hashing",
        ));
    }
    verify_regular_snapshot(path, &file, before)?;
    Ok(hash)
}

pub fn plain_metadata(path: &Path) -> std::io::Result<Option<fs::Metadata>> {
    let metadata = fs::symlink_metadata(path)?;
    Ok((!metadata_is_link(&metadata)).then_some(metadata))
}

pub fn plain_entry_metadata(entry: &fs::DirEntry) -> std::io::Result<Option<fs::Metadata>> {
    let metadata = entry.metadata()?;
    Ok((!metadata_is_link(&metadata)).then_some(metadata))
}

#[cfg(windows)]
fn directory_case_sensitive(path: &Path) -> bool {
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        FileCaseSensitiveInfo, GetFileInformationByHandleEx, FILE_CASE_SENSITIVE_INFO,
        FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    use windows_sys::Win32::System::SystemServices::FILE_CS_FLAG_CASE_SENSITIVE_DIR;

    static CACHE: OnceLock<RwLock<HashMap<PathBuf, bool>>> = OnceLock::new();
    if path.as_os_str().is_empty() {
        return directory_case_sensitive(Path::new("."));
    }
    let cache = CACHE.get_or_init(|| RwLock::new(HashMap::new()));
    if let Ok(guard) = cache.read() {
        if let Some(value) = guard.get(path) {
            return *value;
        }
    }
    let probe = |candidate: &Path| -> std::io::Result<bool> {
        let directory = fs::OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(candidate)?;
        let mut info = FILE_CASE_SENSITIVE_INFO::default();
        let ok = unsafe {
            GetFileInformationByHandleEx(
                directory.as_raw_handle(),
                FileCaseSensitiveInfo,
                (&mut info as *mut FILE_CASE_SENSITIVE_INFO).cast(),
                std::mem::size_of::<FILE_CASE_SENSITIVE_INFO>() as u32,
            )
        };
        if ok == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(info.Flags & FILE_CS_FLAG_CASE_SENSITIVE_DIR != 0)
    };
    let mut candidate = path.to_path_buf();
    let mut exact = true;
    let value = loop {
        if candidate.as_os_str().is_empty() {
            candidate.push(".");
            exact = false;
        }
        match probe(&candidate) {
            Ok(value) => break value,
            Err(error) if error.kind() == ErrorKind::NotFound && candidate.pop() => {
                exact = false;
            }
            Err(error) => return error.kind() != ErrorKind::NotFound,
        }
    };
    if exact {
        if let Ok(mut guard) = cache.write() {
            guard.insert(path.to_path_buf(), value);
        }
    }
    value
}

#[cfg(windows)]
fn ordinal_eq(left: &std::ffi::OsStr, right: &std::ffi::OsStr) -> bool {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Globalization::{CompareStringOrdinal, CSTR_EQUAL};

    if left == right {
        return true;
    }
    let left: Vec<u16> = left.encode_wide().collect();
    let right: Vec<u16> = right.encode_wide().collect();
    let (Ok(left_len), Ok(right_len)) = (i32::try_from(left.len()), i32::try_from(right.len()))
    else {
        return false;
    };
    unsafe {
        CompareStringOrdinal(left.as_ptr(), left_len, right.as_ptr(), right_len, 1) == CSTR_EQUAL
    }
}

#[cfg(windows)]
fn ordinal_key(value: &std::ffi::OsStr) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Globalization::{
        LCMapStringEx, LCMAP_UPPERCASE, LOCALE_NAME_INVARIANT,
    };

    let source: Vec<u16> = value.encode_wide().collect();
    let Ok(source_len) = i32::try_from(source.len()) else {
        return source;
    };
    let needed = unsafe {
        LCMapStringEx(
            LOCALE_NAME_INVARIANT,
            LCMAP_UPPERCASE,
            source.as_ptr(),
            source_len,
            std::ptr::null_mut(),
            0,
            std::ptr::null(),
            std::ptr::null(),
            0,
        )
    };
    if needed <= 0 {
        return source;
    }
    let mut mapped = vec![0_u16; needed as usize];
    let written = unsafe {
        LCMapStringEx(
            LOCALE_NAME_INVARIANT,
            LCMAP_UPPERCASE,
            source.as_ptr(),
            source_len,
            mapped.as_mut_ptr(),
            needed,
            std::ptr::null(),
            std::ptr::null(),
            0,
        )
    };
    if written != needed {
        return source;
    }
    mapped
}

#[cfg(windows)]
pub fn logical_path_key(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    use std::path::Component;

    #[cfg(test)]
    LOGICAL_PATH_KEY_CALLS.with(|calls| calls.set(calls.get() + 1));
    let mut key = Vec::new();
    let mut parent = PathBuf::new();
    for component in path.components() {
        let (tag, spelling) = match component {
            Component::Prefix(prefix) => (1, ordinal_key(prefix.as_os_str())),
            Component::RootDir => (2, ordinal_key(component.as_os_str())),
            Component::CurDir => (3, ordinal_key(component.as_os_str())),
            Component::ParentDir => (4, ordinal_key(component.as_os_str())),
            Component::Normal(value) if directory_case_sensitive(&parent) => {
                (5, value.encode_wide().collect())
            }
            Component::Normal(value) => (5, ordinal_key(value)),
        };
        key.push(tag);
        key.push(u16::try_from(spelling.len()).unwrap_or(u16::MAX));
        key.extend(spelling);
        parent.push(component.as_os_str());
    }
    key
}

#[cfg(all(windows, test))]
thread_local! {
    static LOGICAL_PATH_KEY_CALLS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[cfg(all(windows, test))]
pub(crate) fn reset_logical_path_key_calls() {
    LOGICAL_PATH_KEY_CALLS.with(|calls| calls.set(0));
}

#[cfg(all(windows, test))]
pub(crate) fn logical_path_key_calls() -> usize {
    LOGICAL_PATH_KEY_CALLS.with(std::cell::Cell::get)
}

#[cfg(windows)]
fn matching_prefix(path: &Path, root: &Path) -> Option<usize> {
    use std::path::Component;

    let path_parts: Vec<_> = path.components().collect();
    let root_parts: Vec<_> = root.components().collect();
    if path_parts.len() < root_parts.len() {
        return None;
    }
    if path.starts_with(root) {
        return Some(root_parts.len());
    }
    let mut parent = PathBuf::new();
    for (left, right) in path_parts.iter().zip(&root_parts) {
        let equal = match (left, right) {
            (Component::Normal(left), Component::Normal(right)) => {
                left == right || (!directory_case_sensitive(&parent) && ordinal_eq(left, right))
            }
            _ => ordinal_eq(left.as_os_str(), right.as_os_str()),
        };
        if !equal {
            return None;
        }
        parent.push(left.as_os_str());
    }
    Some(root_parts.len())
}

#[cfg(windows)]
pub fn path_eq(left: &Path, right: &Path) -> bool {
    matching_prefix(left, right).is_some()
        && left.components().count() == right.components().count()
}

#[cfg(not(windows))]
pub fn path_eq(left: &Path, right: &Path) -> bool {
    left == right
}

#[cfg(windows)]
pub fn relative_path(path: &Path, root: &Path) -> Option<PathBuf> {
    let path_parts: Vec<_> = path.components().collect();
    let prefix = matching_prefix(path, root)?;
    Some(path_parts[prefix..].iter().collect())
}

#[cfg(not(windows))]
pub fn relative_path(path: &Path, root: &Path) -> Option<PathBuf> {
    path.strip_prefix(root).ok().map(Path::to_path_buf)
}

#[derive(Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
struct AdapterSource {
    agent: String,
    files: Vec<SourceFile>,
    tokens: Vec<(String, String)>,
    issues: Vec<SourceIssue>,
    complete: bool,
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
struct SourceSnapshot {
    snapshot_version: u32,
    cache_version: u32,
    selection: String,
    adapters: Vec<AdapterSource>,
    complete: bool,
}

/// Decoded current-source preflight. Keeping this view alive lets ingest reuse the exact
/// per-path stamps without deserializing the same snapshot for each consumer.
pub struct SourceSnapshotView {
    snapshot: SourceSnapshot,
}

/// The stable, intentionally small projection consumed by `agrep audit`.
///
/// The opaque serialized snapshot remains the concurrent-change authority. This
/// projection exposes only content paths, exact token rows, collection issues,
/// and completeness; SQLite sidecars stay in the opaque snapshot (and therefore
/// its digest) without being mistaken for independently ingestible content.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceSnapshotAuditView {
    pub paths: Vec<SourceSnapshotAuditPath>,
    pub tokens: Vec<(String, String, String)>,
    pub issues: Vec<SourceIssue>,
    pub complete: bool,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct SourceSnapshotAuditPath {
    pub agent: String,
    pub path: PathBuf,
    pub stat_key: String,
    pub identity_sha256: [u8; 32],
}

#[derive(Clone)]
pub(crate) struct SourceStatStamp {
    pub(crate) mtime_ns: i64,
    pub(crate) len: u64,
    pub(crate) change_token: ChangeToken,
    #[cfg(windows)]
    pub(crate) file_identity: Option<WindowsFileIdentity>,
    pub(crate) content_hashed: bool,
}

type SourceCoverage = (HashSet<PathBuf>, HashMap<String, Vec<(String, String)>>);

impl SourceSnapshotView {
    pub fn complete(&self) -> bool {
        self.snapshot.snapshot_version == SOURCE_SNAPSHOT_VERSION && self.snapshot.complete
    }

    pub fn issues(&self) -> Vec<SourceIssue> {
        self.snapshot
            .adapters
            .iter()
            .flat_map(|adapter| adapter.issues.iter().cloned())
            .collect()
    }

    pub fn audit_view(&self) -> Option<SourceSnapshotAuditView> {
        if (self.snapshot.snapshot_version != SOURCE_SNAPSHOT_VERSION)
            || !self.snapshot.selection.starts_with("audit:")
        {
            return None;
        }
        let mut paths = Vec::new();
        let mut tokens = Vec::new();
        let mut issues = Vec::new();
        for adapter in &self.snapshot.adapters {
            let owner = ADAPTERS
                .iter()
                .copied()
                .find(|candidate| candidate.name() == adapter.agent);
            if let Some(owner) = owner {
                for source in adapter
                    .files
                    .iter()
                    .filter(|source| owner.store_content(&source.path))
                {
                    let Ok(encoded) = bincode::serialize(source) else {
                        issues.push(SourceIssue::new(
                            &adapter.agent,
                            &source.path,
                            "identity-unavailable",
                            "content source identity could not be serialized",
                        ));
                        continue;
                    };
                    let mtime_ms = source
                        .mtime_secs
                        .saturating_mul(1_000)
                        .saturating_add(u64::from(source.mtime_nanos / 1_000_000));
                    paths.push(SourceSnapshotAuditPath {
                        agent: adapter.agent.clone(),
                        path: source.path.clone(),
                        stat_key: format!("s:{mtime_ms}:{}", source.len),
                        identity_sha256: source_audit_identity_sha256(&encoded),
                    });
                }
            } else {
                issues.push(SourceIssue::new(
                    &adapter.agent,
                    Path::new("<registry>"),
                    "adapter-unregistered",
                    "audit snapshot names an adapter outside the current registry",
                ));
            }
            tokens.extend(
                adapter
                    .tokens
                    .iter()
                    .map(|(id, key)| (adapter.agent.clone(), id.clone(), key.clone())),
            );
            issues.extend(adapter.issues.iter().cloned());
        }
        paths.sort();
        paths.dedup();
        tokens.sort();
        tokens.dedup();
        issues.sort();
        issues.dedup();
        let complete = self.complete() && issues.is_empty();
        Some(SourceSnapshotAuditView {
            paths,
            tokens,
            issues,
            complete,
        })
    }

    pub fn cleanly_absent_agents(&self) -> HashSet<String> {
        if self.snapshot.snapshot_version != SOURCE_SNAPSHOT_VERSION {
            return HashSet::new();
        }
        self.snapshot
            .adapters
            .iter()
            .filter(|adapter| {
                adapter.complete
                    && adapter.issues.is_empty()
                    && adapter.files.is_empty()
                    && adapter.tokens.is_empty()
            })
            .map(|adapter| adapter.agent.clone())
            .collect()
    }

    /// Token-store agents whose complete, issue-free observation enumerated zero conversations.
    /// The database may exist: opening cleanly and listing no live sessions is a valid empty
    /// state, unlike a torn/garbage read (those record issues and stay out of this set).
    pub fn cleanly_empty_token_store_agents(&self) -> HashSet<String> {
        if self.snapshot.snapshot_version != SOURCE_SNAPSHOT_VERSION {
            return HashSet::new();
        }
        self.snapshot
            .adapters
            .iter()
            .filter(|adapter| {
                adapter.complete
                    && adapter.issues.is_empty()
                    && adapter.tokens.is_empty()
                    && ADAPTERS.iter().any(|candidate| {
                        candidate.name() == adapter.agent
                            && candidate.fingerprint() == Fingerprint::Token
                    })
            })
            .map(|adapter| adapter.agent.clone())
            .collect()
    }

    pub(crate) fn expectations(&self) -> (HashSet<String>, HashSet<PathBuf>) {
        let mut agents = HashSet::new();
        let mut paths = HashSet::new();
        for adapter in &self.snapshot.adapters {
            if !adapter.files.is_empty() || !adapter.tokens.is_empty() {
                agents.insert(adapter.agent.clone());
            }
            paths.extend(
                adapter
                    .files
                    .iter()
                    .filter(|source| !is_sqlite_sidecar(&source.path))
                    .map(|source| source.path.clone()),
            );
        }
        (agents, paths)
    }

    pub(crate) fn coverage(&self) -> SourceCoverage {
        let mut paths = HashSet::new();
        let mut tokens = HashMap::new();
        for adapter in &self.snapshot.adapters {
            paths.extend(
                adapter
                    .files
                    .iter()
                    .filter(|source| !is_sqlite_sidecar(&source.path))
                    .map(|source| source.path.clone()),
            );
            tokens.insert(adapter.agent.clone(), adapter.tokens.clone());
        }
        (paths, tokens)
    }

    pub(crate) fn stat_stamps(&self) -> HashMap<PathBuf, SourceStatStamp> {
        if !self.complete() {
            return HashMap::new();
        }
        self.snapshot
            .adapters
            .iter()
            .flat_map(|adapter| &adapter.files)
            .map(|source| {
                let mtime_ns = i64::try_from(
                    u128::from(source.mtime_secs) * 1_000_000_000 + u128::from(source.mtime_nanos),
                )
                .unwrap_or(i64::MAX);
                (
                    source.path.clone(),
                    SourceStatStamp {
                        mtime_ns,
                        len: source.len,
                        change_token: source.change_token.clone(),
                        #[cfg(windows)]
                        file_identity: source.file_identity,
                        content_hashed: source.content_hash.is_some(),
                    },
                )
            })
            .collect()
    }
}

pub fn source_snapshot_view(bytes: &[u8]) -> Option<SourceSnapshotView> {
    bincode::deserialize::<SourceSnapshot>(bytes)
        .ok()
        .map(|snapshot| SourceSnapshotView { snapshot })
}

/// A collision-resistant identifier for the exact opaque snapshot bytes.
///
/// This deliberately hashes the serialized form, rather than the public audit
/// projection: WAL-aware sidecars, strong change tokens, cache/schema versions,
/// adapter completeness, and every other boundary field remain authoritative.
pub fn source_snapshot_sha256(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

fn source_audit_identity_sha256(bytes: &[u8]) -> [u8; 32] {
    let mut digest = Sha256::new();
    digest.update(SOURCE_AUDIT_IDENTITY_DOMAIN);
    digest.update((bytes.len() as u64).to_le_bytes());
    digest.update(bytes);
    digest.finalize().into()
}

/// Whether every path represented by this preflight was readable. Partial snapshots may guide
/// a guarded ingest, but must never become the next all-hit permission slip.
pub fn source_snapshot_complete(bytes: &[u8]) -> bool {
    source_snapshot_view(bytes).is_some_and(|snapshot| snapshot.complete())
}

pub fn source_snapshot_issues(bytes: &[u8]) -> Vec<SourceIssue> {
    source_snapshot_view(bytes)
        .map(|snapshot| snapshot.issues())
        .unwrap_or_default()
}

/// Source identities captured by a successful preflight, used only by automatic event repair
/// when the parse cache is unavailable. They distinguish a genuinely absent adapter
/// from a store that preflight observed but whose discovery/read transiently returned empty.
pub fn source_snapshot_expectations(bytes: &[u8]) -> (HashSet<String>, HashSet<PathBuf>) {
    let Some(snapshot) = source_snapshot_view(bytes) else {
        return (HashSet::new(), HashSet::new());
    };
    snapshot.expectations()
}

/// Exact current preflight coverage consumed by the ingest collectors. Stat paths let the cache
/// recover a new file omitted by a transient `read_dir` entry error; Token identities detect a
/// partial DB enumeration even when the outer source snapshot itself succeeds.
pub fn source_snapshot_coverage(bytes: &[u8]) -> SourceCoverage {
    let Some(snapshot) = source_snapshot_view(bytes) else {
        return (HashSet::new(), HashMap::new());
    };
    snapshot.coverage()
}

fn source_file_with_mode(
    agent: &str,
    path: &Path,
    hash_content: bool,
    stable_sqlite: bool,
    audit_identity: bool,
) -> anyhow::Result<Option<SourceFile>> {
    let before = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e).with_context(|| format!("cannot stat {}", path.display())),
    };
    source_file_from_meta_with_mode(
        agent,
        path,
        hash_content,
        stable_sqlite,
        audit_identity,
        before,
    )
}

#[cfg(test)]
#[allow(dead_code)]
fn source_file_from_meta(
    agent: &str,
    path: &Path,
    hash_content: bool,
    _stable_sqlite: bool,
    before: fs::Metadata,
) -> anyhow::Result<Option<SourceFile>> {
    source_file_from_meta_with_mode(agent, path, hash_content, _stable_sqlite, false, before)
}

fn source_file_from_meta_with_mode(
    agent: &str,
    path: &Path,
    hash_content: bool,
    _stable_sqlite: bool,
    audit_identity: bool,
    before: fs::Metadata,
) -> anyhow::Result<Option<SourceFile>> {
    #[cfg(not(windows))]
    let _ = audit_identity;
    if metadata_is_link(&before) || !before.is_file() {
        return Ok(None);
    }
    let modified = before
        .modified()
        .with_context(|| format!("cannot read modified time for {}", path.display()))?
        .duration_since(std::time::UNIX_EPOCH)
        .with_context(|| format!("invalid modified time for {}", path.display()))?;
    #[cfg(windows)]
    let (change_token, file_identity) = if audit_identity {
        audit_metadata_change_token_with_file_identity(path, &before)
            .map(|(token, identity)| (token, Some(identity)))
    } else {
        metadata_change_token_with_file_identity(path, &before)
            .map(|(token, identity)| (token, Some(identity)))
            .or_else(|_| metadata_change_token(path, &before).map(|token| (token, None)))
    }
    .with_context(|| format!("cannot read freshness identity for {}", path.display()))?;
    #[cfg(target_os = "linux")]
    let change_token = if _stable_sqlite {
        crate::ingest::sqlite_stable_change_token(path, &before)
    } else {
        metadata_change_token(path, &before)
    }
    .with_context(|| format!("cannot read freshness identity for {}", path.display()))?;
    #[cfg(all(not(windows), not(target_os = "linux")))]
    let change_token = metadata_change_token(path, &before)
        .with_context(|| format!("cannot read freshness identity for {}", path.display()))?;
    let content_hash = if hash_content {
        Some(
            hash_regular_file_from_meta(path, &before)
                .with_context(|| format!("cannot read {}", path.display()))?,
        )
    } else {
        None
    };
    Ok(Some(SourceFile {
        agent: agent.to_string(),
        path: path.to_path_buf(),
        len: before.len(),
        mtime_secs: modified.as_secs(),
        mtime_nanos: modified.subsec_nanos(),
        change_token,
        #[cfg(windows)]
        file_identity,
        content_hash,
    }))
}

#[derive(Default)]
struct SourceSnapshotTiming {
    traversal: std::time::Duration,
    stamping: std::time::Duration,
    candidates: usize,
}

struct SourceCandidate {
    path: PathBuf,
    metadata: fs::Metadata,
}

struct SourceWalk {
    candidates: Vec<SourceCandidate>,
    issues: Vec<SourceIssue>,
    complete: bool,
    traversal: std::time::Duration,
}

#[cfg(not(windows))]
fn sqlite_snapshot_directory(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let Some(suffix) = name.strip_prefix(".agrep-sqlite-") else {
        return false;
    };
    let mut fields = suffix.split('-');
    let pid = fields
        .next()
        .is_some_and(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()));
    let nonce = fields.next().is_some_and(|value| {
        !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_hexdigit())
    });
    let sequence = fields.next().is_some_and(|value| {
        !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_hexdigit())
    });
    pid && nonce && sequence && fields.next().is_none()
}

#[cfg(windows)]
fn sqlite_snapshot_directory(_path: &Path) -> bool {
    false
}

fn walk_regular_sources(
    adapter: &dyn Adapter,
    path: &Path,
    accepts: impl Fn(&Path) -> bool,
) -> SourceWalk {
    let started = std::time::Instant::now();
    let root = path.to_path_buf();
    let mut pending = vec![(root.clone(), None)];
    let mut candidates = Vec::new();
    let mut issues = Vec::new();
    let mut complete = true;
    while let Some((path, observed)) = pending.pop() {
        let metadata = match observed
            .map(Ok)
            .unwrap_or_else(|| fs::symlink_metadata(&path))
        {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == ErrorKind::NotFound && path == root => continue,
            Err(error) => {
                crate::ingest::warn_source_skip(adapter.name(), &path, &error);
                issues.push(SourceIssue::from_io(adapter.name(), &path, &error));
                complete = false;
                continue;
            }
        };
        if metadata_is_link(&metadata) {
            let reason = "symlink sources are not followed";
            crate::ingest::warn_source_skip(adapter.name(), &path, reason);
            issues.push(SourceIssue::new(
                adapter.name(),
                &path,
                "unsupported-link",
                reason,
            ));
            complete = false;
            continue;
        }
        if metadata.is_file() {
            if accepts(&path) {
                candidates.push(SourceCandidate { path, metadata });
            }
            continue;
        }
        if !metadata.is_dir() {
            if path == root || accepts(&path) {
                let reason = "source is not a regular file or directory";
                crate::ingest::warn_source_skip(adapter.name(), &path, reason);
                issues.push(SourceIssue::new(
                    adapter.name(),
                    &path,
                    "unsupported-file-type",
                    reason,
                ));
                complete = false;
            }
            continue;
        }
        if sqlite_snapshot_directory(&path) {
            continue;
        }
        let entries = match fs::read_dir(&path) {
            Ok(entries) => entries,
            Err(error) => {
                crate::ingest::warn_source_skip(adapter.name(), &path, &error);
                issues.push(SourceIssue::from_io(adapter.name(), &path, &error));
                complete = false;
                continue;
            }
        };
        for entry in entries {
            match entry {
                Ok(entry) => {
                    let path = entry.path();
                    match entry.metadata() {
                        Ok(metadata) => pending.push((path, Some(metadata))),
                        Err(error) => {
                            crate::ingest::warn_source_skip(adapter.name(), &path, &error);
                            issues.push(SourceIssue::from_io(adapter.name(), &path, &error));
                            complete = false;
                        }
                    }
                }
                Err(error) => {
                    crate::ingest::warn_source_skip(adapter.name(), &path, &error);
                    issues.push(SourceIssue::from_io(adapter.name(), &path, &error));
                    complete = false;
                }
            }
        }
    }
    candidates.sort_unstable_by(|left, right| left.path.cmp(&right.path));
    SourceWalk {
        candidates,
        issues,
        complete,
        traversal: started.elapsed(),
    }
}

fn walk_sources(
    adapter: &dyn Adapter,
    path: &Path,
    hash_content: bool,
    audit_identity: bool,
    out: &mut Vec<SourceFile>,
) -> (bool, Vec<SourceIssue>, SourceSnapshotTiming) {
    // Discovery (notably Codex's YYYY/MM/DD tree) has no depth ceiling. Store roots
    // never follow symlinks, so transcript-shaped files outside them stay out of scope.
    let walked = walk_regular_sources(adapter, path, |path| adapter.freshness_content(path));
    let candidate_count = walked.candidates.len();
    let mut complete = walked.complete;
    let mut issues = walked.issues;
    let stamping_started = std::time::Instant::now();
    let agent = adapter.name();
    let kind = adapter.fingerprint();
    let stamped: Vec<_> = walked
        .candidates
        .into_par_iter()
        .map(|candidate| {
            let stable_sqlite = matches!(kind, Fingerprint::Stat | Fingerprint::Token)
                && (sqlite_database(&candidate.path) || is_sqlite_sidecar(&candidate.path));
            let result = source_file_from_meta_with_mode(
                agent,
                &candidate.path,
                hash_content,
                stable_sqlite,
                audit_identity,
                candidate.metadata,
            );
            (candidate.path, result)
        })
        .collect();
    for (path, result) in stamped {
        match result {
            Ok(Some(stamp)) => out.push(stamp),
            Ok(None) => {}
            Err(error) => {
                crate::ingest::warn_source_skip(agent, &path, &error);
                issues.push(SourceIssue::from_anyhow(agent, &path, &error));
                complete = false;
            }
        }
    }
    (
        complete,
        issues,
        SourceSnapshotTiming {
            traversal: walked.traversal,
            stamping: stamping_started.elapsed(),
            candidates: candidate_count,
        },
    )
}

fn sqlite_database(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            [".db", ".sqlite", ".sqlite3", ".vscdb"]
                .iter()
                .any(|suffix| name.ends_with(suffix))
        })
}

fn is_sqlite_sidecar(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            name.ends_with("-wal") || name.ends_with("-shm") || name.ends_with("-journal")
        })
}

#[derive(Clone, Copy)]
enum SnapshotTokenProjection {
    Freshness,
    Intake,
}

fn adapter_source_with_timing(
    adapter: &dyn Adapter,
    token_projection: SnapshotTokenProjection,
) -> anyhow::Result<(AdapterSource, SourceSnapshotTiming)> {
    let kind = adapter.fingerprint();
    let audit_identity = matches!(token_projection, SnapshotTokenProjection::Intake);
    let hash_content = kind == Fingerprint::Always;
    let roots = adapter.freshness_roots();
    let mut files = Vec::new();
    let mut issues = Vec::new();
    let mut complete = true;
    let mut timing = SourceSnapshotTiming::default();
    for root in &roots {
        let (root_complete, mut root_issues, root_timing) =
            walk_sources(adapter, root, hash_content, audit_identity, &mut files);
        complete &= root_complete;
        issues.append(&mut root_issues);
        timing.traversal += root_timing.traversal;
        timing.stamping += root_timing.stamping;
        timing.candidates += root_timing.candidates;
    }
    // Stat adapters need WAL metadata; Token adapters already query exact live row tokens.
    if kind == Fingerprint::Stat {
        let databases: Vec<PathBuf> = files
            .iter()
            .filter(|source| source.agent == adapter.name() && sqlite_database(&source.path))
            .map(|source| source.path.clone())
            .collect();
        for database in databases {
            let wal = crate::ingest::sqlite_sidecar(&database, "-wal");
            match source_file_with_mode(adapter.name(), &wal, false, true, audit_identity) {
                Ok(Some(stamp)) => files.push(stamp),
                Ok(None) => {}
                Err(error) => {
                    crate::ingest::warn_source_skip(adapter.name(), &wal, &error);
                    issues.push(SourceIssue::from_anyhow(adapter.name(), &wal, &error));
                    complete = false;
                }
            }
        }
    }
    files.sort();
    files.dedup();
    let mut tokens = if kind == Fingerprint::Token {
        match match token_projection {
            SnapshotTokenProjection::Freshness => adapter.freshness_tokens(),
            SnapshotTokenProjection::Intake => adapter.intake_tokens(),
        } {
            TokenAvailability::Data(tokens) => tokens,
            TokenAvailability::Empty => Vec::new(),
            TokenAvailability::Unreadable(token_issues) => {
                for issue in token_issues {
                    crate::ingest::warn_source_skip(adapter.name(), &issue.path, &issue.reason);
                    issues.push(SourceIssue::new(
                        adapter.name(),
                        &issue.path,
                        "source-unreadable",
                        issue.reason,
                    ));
                }
                complete = false;
                Vec::new()
            }
        }
    } else {
        Vec::new()
    };
    tokens.sort();
    issues.sort();
    issues.dedup();
    Ok((
        AdapterSource {
            agent: adapter.name().to_string(),
            files,
            tokens,
            issues,
            complete,
        },
        timing,
    ))
}

#[cfg(test)]
fn adapter_source(adapter: &dyn Adapter) -> anyhow::Result<AdapterSource> {
    adapter_source_with_timing(adapter, SnapshotTokenProjection::Freshness)
        .map(|(source, _)| source)
}

fn source_snapshot_with_projection(
    agent: &str,
    selection: String,
    token_projection: SnapshotTokenProjection,
) -> anyhow::Result<Vec<u8>> {
    let started = std::time::Instant::now();
    let selected = select(agent)?;
    let collect = || {
        selected
            .par_iter()
            .map(|adapter| adapter_source_with_timing(*adapter, token_projection))
            .collect::<Vec<anyhow::Result<(AdapterSource, SourceSnapshotTiming)>>>()
    };
    #[cfg(windows)]
    let pieces = match windows_adapter_pool() {
        Some(pool) => pool.install(collect),
        None => collect(),
    };
    #[cfg(not(windows))]
    let pieces = collect();
    let mut adapters = Vec::with_capacity(pieces.len());
    let mut timing = SourceSnapshotTiming::default();
    for piece in pieces {
        let (source, child) = piece?;
        adapters.push(source);
        timing.traversal += child.traversal;
        timing.stamping += child.stamping;
        timing.candidates += child.candidates;
    }
    adapters.sort();
    let complete = adapters.iter().all(|adapter| adapter.complete);
    if std::env::var_os("AGREP_DEBUG").is_some() {
        eprintln!(
            "* [agrep source] snapshot {:.1}ms · traversal-work {:.1}ms · stamp-work {:.1}ms · {} file(s)",
            started.elapsed().as_secs_f64() * 1000.0,
            timing.traversal.as_secs_f64() * 1000.0,
            timing.stamping.as_secs_f64() * 1000.0,
            timing.candidates,
        );
    }
    Ok(bincode::serialize(&SourceSnapshot {
        snapshot_version: SOURCE_SNAPSHOT_VERSION,
        cache_version: crate::ingest_cache::CACHE_VERSION,
        selection,
        adapters,
        complete,
    })?)
}

/// Serialized source state for `agent` (or the complete registry for `all`). The caller stores
/// and compares these opaque bytes; returning an error is conservative and simply disables the
/// early shortcut for that run.
pub fn source_snapshot(agent: &str) -> anyhow::Result<Vec<u8>> {
    source_snapshot_with_projection(agent, agent.to_string(), SnapshotTokenProjection::Freshness)
}

/// One audit boundary over the same strong freshness files as `source_snapshot`, with token
/// rows projected through `intake_tokens()` so their identifiers exactly match the intake book.
pub fn source_audit_snapshot(agent: &str) -> anyhow::Result<Vec<u8>> {
    source_snapshot_with_projection(
        agent,
        format!("audit:{agent}"),
        SnapshotTokenProjection::Intake,
    )
}

/// A store visible on disk but not yet parsed. Presence and a count of session-looking entries
/// are reported as "detected, not yet indexed". Detectors stay separate from `ADAPTERS` so they
/// cannot render as working connectors.
/// A store graduates to ADAPTERS once its format is confirmed and a parser + golden land.
pub struct Detector {
    pub name: &'static str,
    /// Count of session-looking entries under home(), or 0 when the store is absent.
    pub probe: fn() -> usize,
}

pub static DETECTED: &[Detector] = &[
    Detector {
        name: "copilot",
        probe: probe_copilot,
    },
    Detector {
        name: "qwen",
        probe: probe_qwen,
    },
];

/// Names + counts of every store that is present (count > 0). The one probe surface doctor,
/// status --json, and the web setup panel read so "detected, not indexed" stays consistent.
pub fn detected() -> Vec<(&'static str, usize)> {
    DETECTED
        .iter()
        .map(|d| (d.name, (d.probe)()))
        .filter(|(_, n)| *n > 0)
        .collect()
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoreDiagnostic {
    name: &'static str,
    paths: Vec<(String, u64)>,
    issues: Vec<SourceIssue>,
    fingerprint: Fingerprint,
}

impl StoreDiagnostic {
    pub fn name(&self) -> &'static str {
        self.name
    }

    pub fn files(&self) -> usize {
        self.paths.len()
    }

    pub fn newest_mtime_ms(&self) -> u64 {
        self.paths
            .iter()
            .map(|(_, modified)| *modified)
            .max()
            .unwrap_or(0)
    }

    pub fn paths(&self) -> impl Iterator<Item = &str> {
        self.paths.iter().map(|(path, _)| path.as_str())
    }

    pub fn issues(&self) -> &[SourceIssue] {
        &self.issues
    }

    pub fn state(&self) -> &'static str {
        if self.issues.is_empty() {
            "available"
        } else {
            "source-unreadable"
        }
    }

    /// Whether this store's newest file mtime is a clock on transcript content.
    /// False for the single-database adapters, where one file also carries UI
    /// and editor state: any checkpoint rewrites it without a message moving.
    pub fn mtime_tracks_content(&self) -> bool {
        self.fingerprint != Fingerprint::Token
    }
}

pub fn store_diagnostics() -> Vec<StoreDiagnostic> {
    ADAPTERS
        .iter()
        .filter_map(|adapter| store_diagnostic(*adapter))
        .collect()
}

fn store_diagnostic(adapter: &dyn Adapter) -> Option<StoreDiagnostic> {
    let census = store_census(adapter);
    if census.files.is_empty() && census.issues.is_empty() {
        return None;
    }
    Some(StoreDiagnostic {
        name: adapter.name(),
        paths: census
            .files
            .into_iter()
            .map(|(path, modified)| (path.to_string_lossy().into_owned(), modified))
            .collect(),
        issues: census.issues,
        fingerprint: adapter.fingerprint(),
    })
}

/// Per-adapter store presence + newest file mtime: `(name, file_count, newest_mtime_ms)`
/// for every adapter whose store has files. The doctor drift canary joins this against
/// the newest PARSED message per agent - a store that keeps moving while its parses
/// stand still is a broken adapter, and nothing else surfaces that. The join only holds
/// where `mtime_tracks_content`; a shared database moves for reasons no reader authored.
pub fn stores() -> Vec<(&'static str, usize, u64)> {
    store_diagnostics()
        .into_iter()
        .filter(|diagnostic| diagnostic.files() > 0)
        .map(|diagnostic| {
            (
                diagnostic.name(),
                diagnostic.files(),
                diagnostic.newest_mtime_ms(),
            )
        })
        .collect()
}

/// Every file accepted by each adapter's registered store roots and content predicate.
/// The audit census consumes this Rust-owned definition instead of duplicating it in Python.
pub fn store_paths() -> Vec<(&'static str, String)> {
    let mut out = Vec::new();
    for diagnostic in store_diagnostics() {
        for path in diagnostic.paths() {
            out.push((diagnostic.name(), path.to_string()));
        }
    }
    out
}

struct StoreCensus {
    files: Vec<(PathBuf, u64)>,
    issues: Vec<SourceIssue>,
}

fn store_census(adapter: &dyn Adapter) -> StoreCensus {
    let mut files = HashMap::new();
    let mut issues = Vec::new();
    for root in adapter.store_roots() {
        let walked = walk_regular_sources(adapter, &root, |path| adapter.store_content(path));
        issues.extend(walked.issues);
        for candidate in walked.candidates {
            let modified = candidate
                .metadata
                .modified()
                .ok()
                .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|duration| duration.as_millis() as u64)
                .unwrap_or(0);
            files
                .entry(candidate.path)
                .and_modify(|current: &mut u64| *current = (*current).max(modified))
                .or_insert(modified);
        }
    }
    let mut files: Vec<_> = files.into_iter().collect();
    files.sort_unstable_by(|left, right| left.0.cmp(&right.0));
    issues.sort();
    issues.dedup();
    StoreCensus { files, issues }
}

fn count_subdirs(dir: &std::path::Path) -> usize {
    if !std::fs::symlink_metadata(dir)
        .map(|meta| meta.is_dir() && !metadata_is_link(&meta))
        .unwrap_or(false)
    {
        return 0;
    }
    std::fs::read_dir(dir)
        .map(|rd| {
            rd.flatten()
                .filter(|entry| {
                    fs::symlink_metadata(entry.path())
                        .map(|meta| meta.is_dir() && !metadata_is_link(&meta))
                        .unwrap_or(false)
                })
                .count()
        })
        .unwrap_or(0)
}

/// GitHub Copilot CLI: ~/.copilot/session-state/<uuid>/ - one dir per session. Location is
/// confirmed (GitHub Docs); the per-event events.jsonl schema is not, so this stays a stub.
fn probe_copilot() -> usize {
    count_subdirs(&crate::ingest::home().join(".copilot").join("session-state"))
}

/// qwen-code: ~/.qwen/tmp/<projectHash>/chats/*.json (root confirmed from QwenLM/qwen-code
/// packages/core/src/config/storage.ts: QWEN_DIR=".qwen", tmp dir, chats/). A gemini-cli fork,
/// so the shape likely matches the gemini adapter - but not yet confirmed against a real store.
fn probe_qwen() -> usize {
    let tmp = crate::ingest::home().join(".qwen").join("tmp");
    let mut n = 0;
    if !std::fs::symlink_metadata(&tmp)
        .map(|meta| meta.is_dir() && !metadata_is_link(&meta))
        .unwrap_or(false)
    {
        return n;
    }
    if let Ok(rd) = std::fs::read_dir(&tmp) {
        for e in rd.flatten() {
            if !fs::symlink_metadata(e.path())
                .map(|meta| meta.is_dir() && !metadata_is_link(&meta))
                .unwrap_or(false)
            {
                continue;
            }
            let chats_path = e.path().join("chats");
            if !std::fs::symlink_metadata(&chats_path)
                .map(|meta| meta.is_dir() && !metadata_is_link(&meta))
                .unwrap_or(false)
            {
                continue;
            }
            if let Ok(chats) = std::fs::read_dir(chats_path) {
                n += chats
                    .flatten()
                    .filter(|file| {
                        fs::symlink_metadata(file.path())
                            .map(|meta| meta.is_file() && !metadata_is_link(&meta))
                            .unwrap_or(false)
                            && file.path().extension().and_then(|x| x.to_str()) == Some("json")
                    })
                    .count();
            }
        }
    }
    n
}

// Deferred: copilot stays a detection stub (see `probe_copilot`) until a sampled events.jsonl
// exists; aider writes .aider.chat.history.md into project dirs (not home), so discovery would
// harvest candidate cwds from the indexed corpus rather than filesystem scanning.

/// The adapters selected by `agent` ("all" -> every one; a name -> just that one).
fn select(agent: &str) -> anyhow::Result<Vec<&'static dyn Adapter>> {
    if agent == "all" {
        return Ok(ADAPTERS.to_vec());
    }
    match ADAPTERS.iter().find(|a| a.name() == agent) {
        Some(a) => Ok(vec![*a]),
        None => {
            let names: Vec<&str> = ADAPTERS.iter().map(|a| a.name()).collect();
            anyhow::bail!("unknown agent `{agent}` (have: {}, all)", names.join(", "))
        }
    }
}

/// Agent names ingested for a run (the delete-sweep scopes to these so a single-agent run
/// never wipes another agent's event files).
pub fn run_agents(agent: &str) -> Vec<&'static str> {
    match select(agent) {
        Ok(sel) => sel.iter().map(|a| a.name()).collect(),
        Err(_) => vec![],
    }
}

/// Actionable store roots for failures that occur outside an adapter's path-aware reader.
pub fn runtime_issue_roots(agent: &str) -> Vec<(String, PathBuf)> {
    select(agent)
        .map(|adapters| {
            adapters
                .into_iter()
                .map(|adapter| (adapter.name().to_string(), adapter.runtime_issue_root()))
                .collect()
        })
        .unwrap_or_else(|_| vec![(agent.to_string(), crate::ingest::home())])
}

fn canonical_message_cmp(left: &Message, right: &Message) -> std::cmp::Ordering {
    left.reply_chars
        .cmp(&right.reply_chars)
        .then_with(|| left.reply.len().cmp(&right.reply.len()))
        .then_with(|| (!left.model.is_empty()).cmp(&(!right.model.is_empty())))
        .then_with(|| (!left.project.is_empty()).cmp(&(!right.project.is_empty())))
        .then_with(|| (!left.parent.is_empty()).cmp(&(!right.parent.is_empty())))
        .then_with(|| left.side.cmp(&right.side))
        .then_with(|| match (left.ts > 0, right.ts > 0) {
            (true, false) => std::cmp::Ordering::Greater,
            (false, true) => std::cmp::Ordering::Less,
            (true, true) => right.ts.cmp(&left.ts),
            (false, false) => std::cmp::Ordering::Equal,
        })
        .then_with(|| left.reply.as_ref().cmp(right.reply.as_ref()))
        .then_with(|| left.model.as_ref().cmp(right.model.as_ref()))
        .then_with(|| left.project.as_ref().cmp(right.project.as_ref()))
        .then_with(|| left.parent.as_ref().cmp(right.parent.as_ref()))
        .then_with(|| left.who.as_ref().cmp(right.who.as_ref()))
        .then_with(|| left.model_source.as_ref().cmp(right.model_source.as_ref()))
}

fn dedupe_messages_with_repairs(msgs: Vec<Message>) -> (Vec<Message>, HashSet<String>) {
    enum Position {
        One(usize),
        Many(Vec<usize>),
    }

    fn retain_canonical(msgs: &[Message], keep: &mut [bool], prior: &mut usize, index: usize) {
        if canonical_message_cmp(&msgs[index], &msgs[*prior]).is_gt() {
            keep[*prior] = false;
            keep[index] = true;
            *prior = index;
        }
    }

    let mut keep = vec![false; msgs.len()];
    let mut has_turn_collisions = false;
    {
        let mut best: HashMap<(&str, &str, u32), Position> = HashMap::with_capacity(msgs.len());
        for (index, message) in msgs.iter().enumerate() {
            let key = (message.agent, message.session.as_ref(), message.turn);
            match best.entry(key) {
                std::collections::hash_map::Entry::Vacant(entry) => {
                    entry.insert(Position::One(index));
                    keep[index] = true;
                }
                std::collections::hash_map::Entry::Occupied(mut entry) => match entry.get_mut() {
                    Position::One(prior) if message.text == msgs[*prior].text => {
                        retain_canonical(&msgs, &mut keep, prior, index);
                    }
                    Position::One(prior) => {
                        has_turn_collisions = true;
                        keep[index] = true;
                        *entry.get_mut() = Position::Many(vec![*prior, index]);
                    }
                    Position::Many(indices) => {
                        if let Some(prior) = indices
                            .iter_mut()
                            .find(|prior| message.text == msgs[**prior].text)
                        {
                            retain_canonical(&msgs, &mut keep, prior, index);
                        } else {
                            keep[index] = true;
                            indices.push(index);
                        }
                    }
                },
            }
        }
    }
    let msgs = msgs
        .into_iter()
        .zip(keep)
        .filter_map(|(message, keep)| keep.then_some(message))
        .collect();
    if has_turn_collisions {
        repair_turn_collisions(msgs)
    } else {
        (msgs, HashSet::new())
    }
}

#[cfg(test)]
fn dedupe_messages(msgs: Vec<Message>) -> Vec<Message> {
    dedupe_messages_with_repairs(msgs).0
}

fn repair_turn_collisions(mut msgs: Vec<Message>) -> (Vec<Message>, HashSet<String>) {
    let mut positions = HashSet::with_capacity(msgs.len());
    let mut affected: HashSet<(&'static str, std::sync::Arc<str>)> = HashSet::new();
    for message in &msgs {
        if !positions.insert((message.agent, message.session.clone(), message.turn)) {
            affected.insert((message.agent, message.session.clone()));
        }
    }
    if affected.is_empty() {
        return (msgs, HashSet::new());
    }
    let repaired = affected
        .iter()
        .map(|(_, session)| session.to_string())
        .collect();

    let mut sessions: HashMap<(&'static str, std::sync::Arc<str>), Vec<usize>> = HashMap::new();
    for (index, message) in msgs.iter().enumerate() {
        let key = (message.agent, message.session.clone());
        if affected.contains(&key) {
            sessions.entry(key).or_default().push(index);
        }
    }
    for indices in sessions.values_mut() {
        indices.sort_unstable_by(|&left, &right| {
            let a = &msgs[left];
            let b = &msgs[right];
            (a.ts <= 0, a.ts, a.turn, a.text.as_ref()).cmp(&(
                b.ts <= 0,
                b.ts,
                b.turn,
                b.text.as_ref(),
            ))
        });
        for (turn, &index) in indices.iter().enumerate() {
            msgs[index].turn = u32::try_from(turn).expect("one session cannot exceed u32 turns");
        }
    }
    (msgs, repaired)
}

fn dedupe_events(events: Vec<Event>) -> Vec<Event> {
    let mut keep = Vec::with_capacity(events.len());
    {
        let mut seen: HashSet<(&str, &str, &str)> = HashSet::with_capacity(events.len());
        keep.extend(events.iter().map(|event| {
            // An adapter bug must never turn several anonymous calls into one; `collect`
            // normally fills every blank first, but this branch keeps that true regardless.
            event.call_id.is_empty()
                || seen.insert((event.agent, event.session.as_str(), event.call_id.as_str()))
        }));
    }
    events
        .into_iter()
        .zip(keep)
        .filter_map(|(event, keep)| keep.then_some(event))
        .collect()
}

fn hash_event_identity(event: &Event) -> u64 {
    // Stable FNV-1a, deliberately not DefaultHasher (whose output is not a persistence
    // contract). Separators make adjacent variable-width fields unambiguous.
    let mut h = 0xcbf29ce484222325u64;
    let mut add = |bytes: &[u8]| {
        for &byte in bytes.iter().chain(std::iter::once(&0)) {
            h ^= byte as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
    };
    add(event.agent.as_bytes());
    add(event.session.as_bytes());
    add(&event.ts.to_le_bytes());
    add(event.kind.as_bytes());
    add(event.name.as_bytes());
    add(event.input.as_bytes());
    add(event.output.as_bytes());
    add(&[event.ok.map(u8::from).unwrap_or(2)]);
    add(event.child_session.as_bytes());
    h
}

/// Last-resort identity invariant for adapters whose upstream store omitted a call id.
/// Adapters should prefer a native id or a stable source-record id; this fingerprint plus an
/// identical-row ordinal prevents silent conflation while remaining byte-stable on a rerun.
fn fill_anonymous_event_ids(events: &mut [Event]) {
    let mut occurrences: HashMap<(String, String, u64), u32> = HashMap::new();
    for event in events {
        if !event.call_id.trim().is_empty() {
            continue;
        }
        let hash = hash_event_identity(event);
        let occurrence = occurrences
            .entry((event.agent.to_string(), event.session.clone(), hash))
            .or_default();
        event.call_id = format!("agrep-anon-{hash:016x}-{occurrence}");
        *occurrence += 1;
    }
}

type CheckedAdapterRows = (
    &'static str,
    PathBuf,
    Vec<Message>,
    Vec<Event>,
    crate::ingest_cache::ReadOutcome,
    Vec<crate::ingest_cache::SourceReadIssue>,
);

type AdapterDispatch = (Vec<CheckedAdapterRows>, (Vec<Message>, Vec<Event>));

fn ensure_guard_issue(
    cache: &mut IngestCache,
    agent: &'static str,
    root: &Path,
    guard_epoch: u64,
    issue_count: usize,
) {
    if cache.guard_epoch() != guard_epoch && cache.source_read_issues().len() == issue_count {
        cache.record_source_read_issue(
            agent,
            root,
            "source-read-incomplete",
            "the adapter retained last-good rows after an incomplete source read",
        );
    }
}

fn dispatch_adapters(
    full_parse: &[&dyn Adapter],
    cache_driven: &[&dyn Adapter],
    cache: &mut IngestCache,
) -> AdapterDispatch {
    let full_parse: Vec<_> = full_parse
        .iter()
        .copied()
        .filter(|adapter| cache.adapter_required(adapter.name()))
        .collect();
    let cache_driven: Vec<_> = cache_driven
        .iter()
        .copied()
        .filter(|adapter| cache.adapter_required(adapter.name()))
        .collect();
    rayon::join(
        || {
            let mut throwaway = IngestCache::cold();
            let mut named = Vec::new();
            for adapter in &full_parse {
                let (messages, events, healthy, issues) = adapter.collect_checked(&mut throwaway);
                named.push((
                    adapter.name(),
                    adapter.runtime_issue_root(),
                    messages,
                    events,
                    healthy,
                    issues,
                ));
            }
            named
        },
        || {
            let mut messages = Vec::new();
            let mut events = Vec::new();
            for adapter in &cache_driven {
                let guard_epoch = cache.guard_epoch();
                let issue_count = cache.source_read_issues().len();
                // collect_cached serves cached rows even when a store temporarily lists no files.
                let (fresh_messages, fresh_events) = adapter.collect(cache);
                ensure_guard_issue(
                    cache,
                    adapter.name(),
                    &adapter.runtime_issue_root(),
                    guard_epoch,
                    issue_count,
                );
                messages.extend(fresh_messages);
                events.extend(fresh_events);
            }
            (messages, events)
        },
    )
}

#[cfg(any(windows, test))]
fn windows_adapter_threads(available: usize, configured: Option<usize>) -> usize {
    let bounded = available.clamp(1, 12);
    configured
        .filter(|threads| *threads > 0)
        .map_or(bounded, |threads| bounded.min(threads))
}

#[cfg(windows)]
fn windows_adapter_pool() -> Option<&'static rayon::ThreadPool> {
    static POOL: OnceLock<Option<rayon::ThreadPool>> = OnceLock::new();
    POOL.get_or_init(|| {
        let available = std::thread::available_parallelism()
            .map(|threads| threads.get())
            .unwrap_or(1);
        let configured = std::env::var("RAYON_NUM_THREADS")
            .ok()
            .and_then(|value| value.parse().ok())
            .filter(|threads| *threads > 0);
        // Windows hybrid CPUs and file filters regress adapter parsing at full logical width.
        rayon::ThreadPoolBuilder::new()
            .num_threads(windows_adapter_threads(available, configured))
            .thread_name(|index| format!("agrep-adapter-{index}"))
            .build()
            .ok()
    })
    .as_ref()
}

/// Dispatch, retain last-good rows after incomplete reads, and dedupe. Returns unnormalized
/// messages and events for `agent`. Normalization stays in the binary
/// (it is ingest policy, not store reading).
pub fn collect(
    agent: &str,
    cache: &mut IngestCache,
) -> anyhow::Result<(Vec<Message>, Vec<Event>, HashSet<String>)> {
    let debug = std::env::var_os("AGREP_DEBUG").is_some();
    let started = std::time::Instant::now();
    let selected = select(agent)?;
    let cache_driven: Vec<&dyn Adapter> = selected
        .iter()
        .copied()
        .filter(|a| a.fingerprint() != Fingerprint::Always)
        .collect();
    let full_parse: Vec<&dyn Adapter> = selected
        .iter()
        .copied()
        .filter(|a| a.fingerprint() == Fingerprint::Always)
        .collect();

    // Only the cache-driven branch touches `cache`; Always-adapter guarding follows the join.
    #[cfg(windows)]
    let (full_named, (cache_msgs, cache_events)) = match windows_adapter_pool() {
        Some(pool) => pool.install(|| dispatch_adapters(&full_parse, &cache_driven, cache)),
        // Global fallback preserves ingest availability if Windows cannot create the pool.
        None => dispatch_adapters(&full_parse, &cache_driven, cache),
    };
    #[cfg(not(windows))]
    let (full_named, (cache_msgs, cache_events)) =
        dispatch_adapters(&full_parse, &cache_driven, cache);
    let dispatched = started.elapsed();

    // Incomplete full-parse reads retain last-good rows; empty output would delete derived data.
    // A repeated stable empty snapshot confirms real deletion.
    let mut msgs = cache_msgs;
    let mut events = cache_events;
    for (name, root, fresh, fresh_events, healthy, issues) in full_named {
        let issue_count = cache.source_read_issues().len();
        cache.extend_source_read_issues(issues);
        let start = msgs.len();
        let guard_epoch = cache.guard_epoch();
        let (guarded_messages, guard_fired) =
            cache.guard_never_empty(name, fresh, &fresh_events, healthy);
        ensure_guard_issue(cache, name, &root, guard_epoch, issue_count);
        msgs.extend(guarded_messages);
        crate::emit::rows_only(&msgs[start..]);
        // A guarded empty message read is evidence that this Always adapter was unavailable or
        // only partially readable. Its event stream came from the same lossy read and cannot
        // safely overwrite complete per-session files; carry the old event generation forward.
        if !guard_fired {
            events.extend(fresh_events);
        }
    }
    let guarded = started.elapsed();

    // Dedupe by (agent, session, turn, text): Codex resumes replay earlier turns into a
    // new rollout file, so the same message ingests once per file — but compaction shifts
    // turn indices, so only identical text at the same position is the same message.
    let (msgs, repaired_sessions) = dedupe_messages_with_repairs(msgs);
    let messages_deduped = started.elapsed();
    fill_anonymous_event_ids(&mut events);
    debug_assert!(events.iter().all(|event| !event.call_id.is_empty()));
    let events = dedupe_events(events);
    if debug {
        eprintln!(
            "* [agrep ingest] dispatch {:.1}ms · guard {:.1}ms · message-dedupe {:.1}ms · event-dedupe {:.1}ms",
            dispatched.as_secs_f64() * 1000.0,
            (guarded - dispatched).as_secs_f64() * 1000.0,
            (messages_deduped - guarded).as_secs_f64() * 1000.0,
            (started.elapsed() - messages_deduped).as_secs_f64() * 1000.0,
        );
    }
    Ok((msgs, events, repaired_sessions))
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    use super::*;

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct CapabilityContract {
        state: String,
        #[serde(default)]
        reason: String,
    }

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct AdapterContract {
        name: String,
        #[serde(default)]
        aliases: Vec<String>,
        live: CapabilityContract,
        native_resume: CapabilityContract,
        agent_context: AgentContextContract,
        teach: TeachContract,
    }

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct AgentContextContract {
        state: String,
        #[serde(default)]
        reason: String,
        #[serde(default)]
        env_keys: Vec<String>,
    }

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct TeachContract {
        state: String,
        #[serde(default)]
        reason: String,
        #[serde(default)]
        target_ids: Vec<String>,
    }

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct TeachClientContract {
        name: String,
        teach: TeachContract,
    }

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct TeachPathContract {
        base: String,
        parts: Vec<String>,
    }

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct TeachTargetContract {
        id: String,
        label: String,
        kind: String,
        proof: TeachPathContract,
        target: TeachPathContract,
    }

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct RegistryContract {
        version: u32,
        adapters: Vec<AdapterContract>,
        teach_clients: Vec<TeachClientContract>,
        teach_targets: Vec<TeachTargetContract>,
    }

    fn assert_capability(contract: &CapabilityContract) {
        match contract.state.as_str() {
            "supported" => assert!(
                contract.reason.trim().is_empty(),
                "supported capabilities cannot carry an unsupported reason"
            ),
            "unsupported" => assert!(
                !contract.reason.trim().is_empty(),
                "unsupported capabilities need a reason"
            ),
            state => panic!("unknown capability state {state:?}"),
        }
    }

    fn assert_agent_name(name: &str) {
        let mut bytes = name.bytes();
        assert!(bytes.next().is_some_and(|byte| byte.is_ascii_lowercase()));
        assert!(bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        }));
    }

    fn assert_agent_context(contract: &AgentContextContract, seen: &mut HashSet<String>) {
        match contract.state.as_str() {
            "supported" => {
                assert!(contract.reason.trim().is_empty());
                assert!(!contract.env_keys.is_empty());
            }
            "unsupported" => {
                assert!(!contract.reason.trim().is_empty());
                assert!(contract.env_keys.is_empty());
            }
            state => panic!("unknown capability state {state:?}"),
        }
        for key in &contract.env_keys {
            let mut bytes = key.bytes();
            assert!(bytes.next().is_some_and(|byte| byte.is_ascii_uppercase()));
            assert!(bytes
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_'));
            assert!(
                seen.insert(key.clone()),
                "duplicate agent-context key {key:?}"
            );
        }
    }

    fn assert_teach_path(path: &TeachPathContract) {
        assert!(matches!(path.base.as_str(), "home" | "opencode_data"));
        assert!(path.parts.iter().all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && !part.contains('/')
                && !part.contains('\\')
        }));
    }

    fn assert_teach_target(target: &TeachTargetContract) {
        let mut id = target.id.bytes();
        assert!(id.next().is_some_and(|byte| byte.is_ascii_lowercase()));
        assert!(id.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        }));
        assert!(!target.label.is_empty());
        assert_eq!(target.label, target.label.trim());
        assert!(!target.label.chars().any(char::is_control));
        assert!(matches!(target.kind.as_str(), "markdown" | "skill"));
        assert_teach_path(&target.proof);
        assert_teach_path(&target.target);
    }

    fn assert_teach(
        contract: &TeachContract,
        target_ids: &HashSet<String>,
        referenced: &mut HashSet<String>,
    ) {
        match contract.state.as_str() {
            "supported" => {
                assert!(contract.reason.trim().is_empty());
                assert!(!contract.target_ids.is_empty());
            }
            "unsupported" => {
                assert!(!contract.reason.trim().is_empty());
                assert!(contract.target_ids.is_empty());
            }
            state => panic!("unknown capability state {state:?}"),
        }
        let mut seen = HashSet::new();
        for target_id in &contract.target_ids {
            assert!(target_ids.contains(target_id));
            assert!(
                seen.insert(target_id),
                "duplicate teach target {target_id:?}"
            );
            referenced.insert(target_id.clone());
        }
    }

    fn registry_contract() -> RegistryContract {
        serde_json::from_str(include_str!("../../../../py/hookless/agent_registry.json")).unwrap()
    }

    fn assert_registry_contract(contract: &RegistryContract) {
        assert_eq!(contract.version, 2);
        let expected: Vec<_> = ADAPTERS.iter().map(|adapter| adapter.name()).collect();
        let actual: Vec<_> = contract
            .adapters
            .iter()
            .map(|adapter| adapter.name.as_str())
            .collect();
        assert_eq!(actual, expected);
        assert_eq!(
            actual.iter().copied().collect::<HashSet<_>>().len(),
            actual.len(),
            "adapter names must be unique"
        );
        let mut input_names = HashSet::new();
        for adapter in &contract.adapters {
            assert_agent_name(&adapter.name);
            assert!(input_names.insert(adapter.name.as_str()));
            for alias in &adapter.aliases {
                assert_agent_name(alias);
                assert!(
                    input_names.insert(alias.as_str()),
                    "adapter aliases must be globally unique"
                );
            }
        }
        for client in &contract.teach_clients {
            assert_agent_name(&client.name);
            assert!(
                input_names.insert(client.name.as_str()),
                "teach clients must be unique and distinct from adapters"
            );
        }
        let mut teach_target_ids = HashSet::new();
        for target in &contract.teach_targets {
            assert_teach_target(target);
            assert!(
                teach_target_ids.insert(target.id.clone()),
                "duplicate teach target {:?}",
                target.id
            );
        }
        assert!(!teach_target_ids.is_empty());
        let mut context_keys = HashSet::new();
        let mut referenced_targets = HashSet::new();
        for adapter in &contract.adapters {
            assert_capability(&adapter.live);
            assert_capability(&adapter.native_resume);
            assert_agent_context(&adapter.agent_context, &mut context_keys);
            assert_teach(&adapter.teach, &teach_target_ids, &mut referenced_targets);
        }
        for client in &contract.teach_clients {
            assert_teach(&client.teach, &teach_target_ids, &mut referenced_targets);
        }
        assert_eq!(referenced_targets, teach_target_ids);
    }

    #[test]
    fn hookless_registry_matches_registered_adapters() {
        assert_registry_contract(&registry_contract());
    }

    fn assert_invalid_registry_value(value: serde_json::Value) {
        let contract: RegistryContract = serde_json::from_value(value).unwrap();
        assert!(std::panic::catch_unwind(|| assert_registry_contract(&contract)).is_err());
    }

    #[test]
    fn hookless_registry_rejects_silent_teach_target_deletion_and_disablement() {
        let raw = include_str!("../../../../py/hookless/agent_registry.json");
        let mut deleted: serde_json::Value = serde_json::from_str(raw).unwrap();
        deleted["teach_targets"].as_array_mut().unwrap().remove(0);
        assert_invalid_registry_value(deleted);

        let mut disabled: serde_json::Value = serde_json::from_str(raw).unwrap();
        disabled["adapters"][0]["teach"] = serde_json::json!({
            "state": "unsupported",
            "reason": "fixture has no writable instruction surface"
        });
        assert_invalid_registry_value(disabled);
    }

    #[test]
    fn hookless_registry_rejects_noncanonical_or_control_labels() {
        let raw = include_str!("../../../../py/hookless/agent_registry.json");
        for label in [" leading", "trailing ", "delete\u{7f}"] {
            let mut value: serde_json::Value = serde_json::from_str(raw).unwrap();
            value["teach_targets"][0]["label"] = serde_json::Value::String(label.into());
            assert_invalid_registry_value(value);
        }
    }

    fn message(session: &str, turn: u32, text: &str) -> Message {
        crate::model::RawMessage {
            agent: "codex",
            project: "project".into(),
            session: session.into(),
            ts: 1,
            turn,
            text: text.into(),
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
        }
        .freeze()
    }

    #[test]
    fn guarded_adapter_without_finer_issue_records_actionable_root() {
        let root = std::env::temp_dir().join("agrep-guarded-adapter-root");
        let mut cache = IngestCache::cold();
        cache.guard_never_empty(
            "cline",
            vec![message("session", 0, "last good")],
            &[],
            crate::ingest_cache::ReadOutcome::Complete,
        );
        let guard_epoch = cache.guard_epoch();
        let issue_count = cache.source_read_issues().len();
        cache.guard_never_empty(
            "cline",
            Vec::new(),
            &[],
            crate::ingest_cache::ReadOutcome::Skipped,
        );
        ensure_guard_issue(&mut cache, "cline", &root, guard_epoch, issue_count);
        assert_eq!(cache.source_read_issues().len(), 1);
        assert_eq!(cache.source_read_issues()[0].path, root);
    }

    fn event(session: &str, call_id: &str, name: &str) -> Event {
        Event {
            agent: "codex",
            session: session.into(),
            ts: 1,
            kind: "tool",
            name: name.into(),
            input: String::new(),
            output: String::new(),
            input_chars: 0,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id: call_id.into(),
            child_session: String::new(),
        }
    }

    #[test]
    fn audit_view_filters_sidecars_but_exact_digest_retains_them() {
        let source_file = |agent: &str, path: &str, change_token| SourceFile {
            agent: agent.into(),
            path: PathBuf::from(path),
            len: 1,
            mtime_secs: 1,
            mtime_nanos: 0,
            change_token: ChangeToken::Metadata(change_token),
            #[cfg(windows)]
            file_identity: None,
            content_hash: None,
        };
        let build = |wal_token, chat_token| {
            bincode::serialize(&SourceSnapshot {
                snapshot_version: SOURCE_SNAPSHOT_VERSION,
                cache_version: crate::ingest_cache::CACHE_VERSION,
                selection: "audit:all".into(),
                adapters: vec![
                    AdapterSource {
                        agent: "kimi".into(),
                        files: vec![
                            source_file("kimi", "chat.jsonl", chat_token),
                            source_file("kimi", "kimi.json", 1),
                        ],
                        tokens: Vec::new(),
                        issues: Vec::new(),
                        complete: true,
                    },
                    AdapterSource {
                        agent: "opencode".into(),
                        files: vec![
                            source_file("opencode", "opencode.db", 1),
                            source_file("opencode", "opencode.db-wal", wal_token),
                        ],
                        tokens: Vec::new(),
                        issues: Vec::new(),
                        complete: true,
                    },
                    AdapterSource {
                        agent: "cursor".into(),
                        files: Vec::new(),
                        tokens: vec![("intake-session".into(), "token".into())],
                        issues: Vec::new(),
                        complete: true,
                    },
                ],
                complete: true,
            })
            .unwrap()
        };

        let first = build(1, 1);
        let second = build(2, 1);
        let view = source_snapshot_view(&first).unwrap().audit_view().unwrap();
        assert_eq!(
            view.paths
                .iter()
                .map(|source| (source.agent.as_str(), source.path.as_path()))
                .collect::<Vec<_>>(),
            vec![
                ("kimi", Path::new("chat.jsonl")),
                ("opencode", Path::new("opencode.db")),
            ],
            "freshness-only metadata and SQLite sidecars are not ingest content"
        );
        assert_eq!(view.paths[0].stat_key, "s:1000:1");
        assert_ne!(view.paths[0].identity_sha256, [0; 32]);
        let rewritten = source_snapshot_view(&build(1, 2))
            .unwrap()
            .audit_view()
            .unwrap();
        assert_eq!(view.paths[0].stat_key, rewritten.paths[0].stat_key);
        assert_ne!(
            view.paths[0].identity_sha256, rewritten.paths[0].identity_sha256,
            "same-size restored-mtime change identity must move the witness"
        );
        assert_eq!(
            view.tokens,
            vec![(
                "cursor".to_string(),
                "intake-session".to_string(),
                "token".to_string()
            )]
        );
        assert!(view.issues.is_empty());
        assert!(view.complete);
        assert_ne!(
            source_snapshot_sha256(&first),
            source_snapshot_sha256(&second),
            "a hidden WAL change must move the exact boundary digest"
        );
        let known = source_snapshot_sha256(b"abc")
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(
            known,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let mut normal: SourceSnapshot = bincode::deserialize(&first).unwrap();
        normal.selection = "all".into();
        assert!(source_snapshot_view(&bincode::serialize(&normal).unwrap())
            .unwrap()
            .audit_view()
            .is_none());
    }

    #[cfg(windows)]
    #[test]
    fn source_snapshot_roundtrips_unpaired_utf16_paths() {
        use std::ffi::OsString;
        use std::os::windows::ffi::{OsStrExt, OsStringExt};

        let path = PathBuf::from(OsString::from_wide(&[0x43, 0x3a, 0x5c, 0xd800, 0x61]));
        let snapshot = SourceSnapshot {
            snapshot_version: SOURCE_SNAPSHOT_VERSION,
            cache_version: crate::ingest_cache::CACHE_VERSION,
            selection: "all".into(),
            adapters: vec![AdapterSource {
                agent: "codex".into(),
                files: vec![SourceFile {
                    agent: "codex".into(),
                    path,
                    len: 1,
                    mtime_secs: 1,
                    mtime_nanos: 0,
                    change_token: ChangeToken::Metadata(1),
                    file_identity: None,
                    content_hash: None,
                }],
                tokens: Vec::new(),
                issues: Vec::new(),
                complete: true,
            }],
            complete: true,
        };
        let encoded = bincode::serialize(&snapshot).unwrap();
        let decoded: SourceSnapshot = bincode::deserialize(&encoded).unwrap();
        let units: Vec<_> = decoded.adapters[0].files[0]
            .path
            .as_os_str()
            .encode_wide()
            .collect();
        assert_eq!(units, [0x43, 0x3a, 0x5c, 0xd800, 0x61]);
    }

    #[test]
    fn windows_file_size_from_parts_preserves_the_high_word() {
        assert_eq!(
            windows_file_size_from_parts(2, 3).unwrap(),
            (2_u64 << 32) | 3
        );
        assert_eq!(
            windows_file_size_from_parts(0x7fff_ffff, u32::MAX).unwrap(),
            i64::MAX as u64
        );
        let error = windows_file_size_from_parts(0x8000_0000, 0).unwrap_err();
        assert_eq!(error.kind(), ErrorKind::InvalidData);
        assert_eq!(error.to_string(), "invalid negative source size");
    }

    #[cfg(windows)]
    #[test]
    fn audit_windows_fallback_uses_change_time_not_content_hash() {
        let identity = WindowsFileIdentity {
            kind: 1,
            volume: 7,
            id: [9; 16],
        };
        let before = WindowsHandleState {
            identity,
            legacy_volume: 7,
            legacy_inode: 9,
            size: 4096,
            last_write: 123,
            change_time: 456,
            attributes: 0,
        };
        let after = WindowsHandleState {
            change_time: 457,
            ..before
        };
        assert_eq!(
            windows_audit_change_token(before),
            ChangeToken::Metadata(456)
        );
        assert_eq!(
            windows_audit_change_token(after),
            ChangeToken::Metadata(457)
        );
        assert_ne!(
            windows_audit_change_token(before),
            windows_audit_change_token(after),
            "same-size restored-last-write ChangeTime movement must invalidate audit"
        );
        let state = WindowsHandleState {
            legacy_volume: 11,
            legacy_inode: 13,
            size: 17,
            last_write: 116_444_736_000_000_123,
            change_time: 456,
            ..before
        };
        assert_eq!(
            windows_reader_identity(state).unwrap(),
            RegularFileReaderIdentity {
                len: 17,
                modified_ns: 12_300,
                changed_ns: 45_600,
                device: 11,
                inode: 13,
            }
        );
    }

    /// Extent movement changes the token; only an identity swap invalidates the read.
    #[cfg(windows)]
    #[test]
    fn same_open_file_accepts_extent_movement_rejects_identity_swap() {
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;

        let identity = WindowsFileIdentity {
            kind: 1,
            volume: 7,
            id: [9; 16],
        };
        let base = WindowsHandleState {
            identity,
            legacy_volume: 7,
            legacy_inode: 9,
            size: 4096,
            last_write: 100,
            change_time: 200,
            attributes: 0,
        };

        // A live writer can move extents between token capture and the identity check.
        let grown = WindowsHandleState { size: 8192, ..base };
        assert!(
            same_open_file(&base, &grown),
            "size growth must not mark a live writer as a different file"
        );
        let updated = WindowsHandleState {
            last_write: 150,
            change_time: 250,
            ..base
        };
        assert!(
            same_open_file(&base, &updated),
            "timestamp movement must not mark a live writer as a different file"
        );
        let grown_and_updated = WindowsHandleState {
            size: 8192,
            last_write: 150,
            change_time: 250,
            ..base
        };
        assert!(
            same_open_file(&base, &grown_and_updated),
            "combined extent movement must not mark a live writer as a different file"
        );

        let replaced = WindowsHandleState {
            identity: WindowsFileIdentity {
                kind: 1,
                volume: 7,
                id: [0xff; 16],
            },
            ..base
        };
        assert!(
            !same_open_file(&base, &replaced),
            "file identity change must be detected as a replacement"
        );

        let legacy_replaced = WindowsHandleState {
            legacy_inode: 9999,
            ..base
        };
        assert!(
            !same_open_file(&base, &legacy_replaced),
            "legacy inode change must be detected as a replacement"
        );

        // A new reparse attribute is a structural identity change.
        let reparse = WindowsHandleState {
            attributes: FILE_ATTRIBUTE_REPARSE_POINT,
            ..base
        };
        assert!(
            !same_open_file(&base, &reparse),
            "reparse point attribute must be detected as a replacement"
        );
    }

    struct DispatchAdapter {
        name: &'static str,
        kind: Fingerprint,
        messages: Vec<Message>,
        events: Vec<Event>,
        observed_width: Arc<AtomicUsize>,
    }

    impl Adapter for DispatchAdapter {
        fn name(&self) -> &'static str {
            self.name
        }

        fn fingerprint(&self) -> Fingerprint {
            self.kind
        }

        fn collect(&self, _cache: &mut IngestCache) -> (Vec<Message>, Vec<Event>) {
            self.observed_width
                .fetch_max(rayon::current_num_threads(), Ordering::Relaxed);
            (
                self.messages
                    .par_iter()
                    .map(|message| {
                        self.observed_width
                            .fetch_max(rayon::current_num_threads(), Ordering::Relaxed);
                        message.clone()
                    })
                    .collect(),
                self.events.par_iter().cloned().collect(),
            )
        }

        fn store_roots(&self) -> Vec<PathBuf> {
            Vec::new()
        }
    }

    #[test]
    fn windows_adapter_pool_bounds_hybrid_logical_cores() {
        assert_eq!(windows_adapter_threads(0, None), 1);
        assert_eq!(windows_adapter_threads(1, None), 1);
        assert_eq!(windows_adapter_threads(8, None), 8);
        assert_eq!(windows_adapter_threads(24, None), 12);
        assert_eq!(windows_adapter_threads(128, None), 12);
        assert_eq!(windows_adapter_threads(24, Some(0)), 12);
        assert_eq!(windows_adapter_threads(24, Some(8)), 8);
        assert_eq!(windows_adapter_threads(24, Some(48)), 12);
    }

    #[test]
    fn adapter_dispatch_pool_preserves_both_branches() {
        let always_width = Arc::new(AtomicUsize::new(0));
        let cached_width = Arc::new(AtomicUsize::new(0));
        let always = DispatchAdapter {
            name: "always-test",
            kind: Fingerprint::Always,
            messages: (0..8)
                .map(|turn| message("always", turn, &format!("always row {turn}")))
                .collect(),
            events: vec![event("always", "always-call", "always event")],
            observed_width: Arc::clone(&always_width),
        };
        let cached = DispatchAdapter {
            name: "cached-test",
            kind: Fingerprint::Stat,
            messages: (0..8)
                .map(|turn| message("cached", turn, &format!("cached row {turn}")))
                .collect(),
            events: vec![event("cached", "cached-call", "cached event")],
            observed_width: Arc::clone(&cached_width),
        };
        let full_parse: [&dyn Adapter; 1] = [&always];
        let cache_driven: [&dyn Adapter; 1] = [&cached];
        let mut direct_cache = IngestCache::cold();
        let direct = dispatch_adapters(&full_parse, &cache_driven, &mut direct_cache);
        let signature = |result: &AdapterDispatch| {
            let (full_rows, (cached_messages, cached_events)) = result;
            (
                full_rows
                    .iter()
                    .map(|(name, _, messages, events, outcome, _)| {
                        (
                            *name,
                            messages
                                .iter()
                                .map(|message| message.text.to_string())
                                .collect::<Vec<_>>(),
                            events
                                .iter()
                                .map(|event| event.name.clone())
                                .collect::<Vec<_>>(),
                            *outcome,
                        )
                    })
                    .collect::<Vec<_>>(),
                cached_messages
                    .iter()
                    .map(|message| message.text.to_string())
                    .collect::<Vec<_>>(),
                cached_events
                    .iter()
                    .map(|event| event.name.clone())
                    .collect::<Vec<_>>(),
            )
        };
        let run = |width| {
            always_width.store(0, Ordering::Relaxed);
            cached_width.store(0, Ordering::Relaxed);
            let pool = rayon::ThreadPoolBuilder::new()
                .num_threads(width)
                .build()
                .unwrap();
            let mut cache = IngestCache::cold();
            let result = pool.install(|| dispatch_adapters(&full_parse, &cache_driven, &mut cache));
            let observed = (
                always_width.load(Ordering::Relaxed),
                cached_width.load(Ordering::Relaxed),
            );
            (result, observed)
        };

        let (single, single_width) = run(1);
        let (pooled, pooled_width) = run(2);
        let (wide, wide_width) = run(12);
        assert_eq!(single_width, (1, 1));
        assert_eq!(pooled_width, (2, 2));
        assert_eq!(wide_width, (12, 12));
        assert_eq!(signature(&direct), signature(&pooled));
        assert_eq!(signature(&single), signature(&wide));

        let outer = rayon::ThreadPoolBuilder::new()
            .num_threads(7)
            .build()
            .unwrap();
        let (before, nested, nested_width, after) = outer.install(|| {
            let before = rayon::current_num_threads();
            let (nested, nested_width) = run(3);
            let after = rayon::current_num_threads();
            (before, nested, nested_width, after)
        });
        assert_eq!((before, after), (7, 7));
        assert_eq!(nested_width, (3, 3));
        assert_eq!(signature(&direct), signature(&nested));
    }

    #[test]
    fn complete_snapshot_skips_only_absent_adapters_without_prior_material() {
        let absent_width = Arc::new(AtomicUsize::new(0));
        let present_width = Arc::new(AtomicUsize::new(0));
        let absent = DispatchAdapter {
            name: "absent-test",
            kind: Fingerprint::Always,
            messages: vec![message("absent", 0, "must not run")],
            events: Vec::new(),
            observed_width: Arc::clone(&absent_width),
        };
        let present = DispatchAdapter {
            name: "present-test",
            kind: Fingerprint::Stat,
            messages: vec![message("present", 0, "present row")],
            events: Vec::new(),
            observed_width: Arc::clone(&present_width),
        };
        let mut cache = IngestCache::cold();
        cache.set_current_source_agents(HashSet::from(["present-test".to_string()]));
        let result = dispatch_adapters(&[&absent], &[&present], &mut cache);
        assert!(result.0.is_empty());
        assert_eq!((result.1).0.len(), 1);
        assert_eq!(absent_width.load(Ordering::Relaxed), 0);
        assert!(present_width.load(Ordering::Relaxed) > 0);
    }

    #[test]
    fn absent_adapter_with_prior_material_still_runs() {
        let observed = Arc::new(AtomicUsize::new(0));
        let adapter = DispatchAdapter {
            name: "codex",
            kind: Fingerprint::Always,
            messages: Vec::new(),
            events: Vec::new(),
            observed_width: Arc::clone(&observed),
        };
        let mut cache = IngestCache::cold();
        cache.guard_never_empty(
            "codex",
            vec![message("prior", 0, "prior row")],
            &[],
            crate::ingest_cache::ReadOutcome::Complete,
        );
        cache.set_current_source_agents(HashSet::new());
        let result = dispatch_adapters(&[&adapter], &[], &mut cache);
        assert_eq!(result.0.len(), 1);
        assert!(observed.load(Ordering::Relaxed) > 0);
    }

    #[test]
    fn borrowed_dedupe_is_stable_first_wins() {
        let messages = dedupe_messages(vec![
            message("a", 0, "first"),
            message("b", 0, "middle"),
            message("a", 0, "first"), // byte-identical replay at the same position
            message("a", 1, "last"),
        ]);
        assert_eq!(
            messages.iter().map(|m| m.text.as_ref()).collect::<Vec<_>>(),
            ["first", "middle", "last"]
        );

        let events = dedupe_events(vec![
            event("a", "call-1", "first"),
            event("b", "call-1", "middle"),
            event("a", "call-1", "duplicate"),
            event("a", "call-2", "last"),
        ]);
        assert_eq!(
            events
                .iter()
                .map(|event| event.name.as_str())
                .collect::<Vec<_>>(),
            ["first", "middle", "last"]
        );
    }

    #[test]
    fn colliding_turns_with_distinct_texts_both_survive() {
        // Compaction-resume replays shift surviving prompts to lower turn indices,
        // so distinct real prompts can collide at one (session, turn).
        let messages = dedupe_messages(vec![
            message("a", 0, "ship the fix"),
            message("a", 0, "now write the changelog"),
            message("a", 0, "ship the fix"),
        ]);
        assert_eq!(messages.len(), 2);
        assert_eq!(
            messages
                .iter()
                .map(|m| m.turn)
                .collect::<HashSet<_>>()
                .len(),
            2
        );
        let assigned: HashMap<&str, u32> = messages
            .iter()
            .map(|message| (message.text.as_ref(), message.turn))
            .collect();
        let reversed = dedupe_messages(vec![
            message("a", 0, "now write the changelog"),
            message("a", 0, "ship the fix"),
        ]);
        assert_eq!(
            assigned,
            reversed
                .iter()
                .map(|message| (message.text.as_ref(), message.turn))
                .collect()
        );
    }

    #[test]
    fn replay_dedupe_selects_complete_content_independent_of_input_order() {
        let mut short = message("a", 0, "same prompt");
        short.reply = "short".into();
        short.reply_chars = 5;
        let mut complete = message("a", 0, "same prompt");
        complete.reply = "complete reply".into();
        complete.reply_chars = 14;
        for rows in [
            vec![short.clone(), complete.clone()],
            vec![complete.clone(), short.clone()],
        ] {
            let chosen = dedupe_messages(rows);
            assert_eq!(chosen.len(), 1);
            assert_eq!(chosen[0].reply.as_ref(), "complete reply");
        }
    }

    #[test]
    fn repaired_turns_publish_unique_ids_with_their_own_replies() {
        let mut first = message("collision", 0, "first prompt");
        first.reply = "first reply".into();
        first.reply_chars = 11;
        let mut second = message("collision", 0, "second prompt");
        second.reply = "second reply".into();
        second.reply_chars = 12;
        let messages = dedupe_messages(vec![first, second]);
        let root = std::env::temp_dir().join(format!(
            "agrep-collision-publication-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let message_path = root.join("messages.jsonl");
        let reply_path = root.join("replies.jsonl");
        crate::cache::write_messages(&messages, &message_path).unwrap();
        crate::cache::write_replies(&messages, &reply_path).unwrap();

        let rows: Vec<serde_json::Value> = fs::read_to_string(message_path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        let replies: HashMap<String, String> = fs::read_to_string(reply_path)
            .unwrap()
            .lines()
            .map(|line| {
                let row: serde_json::Value = serde_json::from_str(line).unwrap();
                (
                    row["id"].as_str().unwrap().to_string(),
                    row["reply"].as_str().unwrap().to_string(),
                )
            })
            .collect();
        assert_eq!(
            rows.iter()
                .map(|row| &row["id"])
                .collect::<HashSet<_>>()
                .len(),
            2
        );
        for row in rows {
            let expected = if row["text"] == "first prompt" {
                "first reply"
            } else {
                "second reply"
            };
            assert_eq!(replies[row["id"].as_str().unwrap()], expected);
        }
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn collision_free_sparse_turns_remain_byte_stable() {
        let messages = dedupe_messages(vec![
            message("clean", 2, "two"),
            message("clean", 7, "seven"),
        ]);
        assert_eq!(
            messages
                .iter()
                .map(|message| message.turn)
                .collect::<Vec<_>>(),
            [2, 7]
        );
    }

    #[test]
    fn anonymous_events_are_stable_and_never_conflated() {
        let mut first = vec![
            event("a", "", "same"),
            event("a", "", "same"),
            event("a", "", "different"),
        ];
        let mut second = first.clone();
        fill_anonymous_event_ids(&mut first);
        fill_anonymous_event_ids(&mut second);
        assert_eq!(
            first.iter().map(|event| &event.call_id).collect::<Vec<_>>(),
            second
                .iter()
                .map(|event| &event.call_id)
                .collect::<Vec<_>>()
        );
        assert!(first.iter().all(|event| !event.call_id.is_empty()));
        assert_eq!(dedupe_events(first).len(), 3);

        // Defense in depth: raw blank ids also remain distinct if dedupe is called without
        // the normal invariant-filling stage.
        assert_eq!(
            dedupe_events(vec![event("a", "", "one"), event("a", "", "two")]).len(),
            2
        );
    }

    struct SnapshotAdapter {
        root: PathBuf,
        kind: Fingerprint,
        tokens: TokenAvailability,
    }

    impl Adapter for SnapshotAdapter {
        fn name(&self) -> &'static str {
            "snapshot-test"
        }
        fn fingerprint(&self) -> Fingerprint {
            self.kind
        }
        fn collect(&self, _cache: &mut IngestCache) -> (Vec<Message>, Vec<Event>) {
            (Vec::new(), Vec::new())
        }
        fn store_roots(&self) -> Vec<PathBuf> {
            vec![self.root.clone()]
        }
        fn freshness_tokens(&self) -> TokenAvailability {
            self.tokens.clone()
        }
    }

    fn snapshot_root(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agrep-source-snapshot-{label}-{}",
            std::process::id()
        ))
    }

    #[cfg(unix)]
    fn fifo(path: &Path) {
        let status = std::process::Command::new("mkfifo")
            .arg(path)
            .status()
            .unwrap();
        assert!(status.success());
    }

    #[cfg(unix)]
    #[test]
    fn bounded_readers_reject_fifos_and_regular_to_fifo_swaps() {
        let root = snapshot_root("bounded-fifo");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();

        let direct = root.join("direct");
        fifo(&direct);
        assert!(read_bounded_regular_file(&direct, 1024).is_err());

        let swapped = root.join("swapped");
        fs::write(&swapped, b"regular").unwrap();
        let before = fs::symlink_metadata(&swapped).unwrap();
        fs::rename(&swapped, root.join("old")).unwrap();
        fifo(&swapped);
        assert!(read_bounded_regular_file_from_meta(&swapped, 1024, before).is_err());

        let edge = root.join("edge");
        fs::write(&edge, b"regular").unwrap();
        let before = fs::symlink_metadata(&edge).unwrap();
        fs::rename(&edge, root.join("edge-old")).unwrap();
        fifo(&edge);
        assert!(regular_file_edge_snapshot_from_meta(&edge, 4, before).is_err());

        let post_read = root.join("post-read");
        let post_read_old = root.join("post-read-old");
        fs::write(&post_read, b"regular").unwrap();
        let before = fs::symlink_metadata(&post_read).unwrap();
        let result =
            read_bounded_regular_file_snapshot_from_meta_with(&post_read, 1024, before, || {
                fs::rename(&post_read, &post_read_old).unwrap();
                std::os::unix::fs::symlink(&post_read_old, &post_read).unwrap();
            });
        assert!(result.is_err());
        let _ = fs::remove_dir_all(root);
    }

    /// Growth is normal and served; losing the bytes just read is not.
    #[test]
    fn growing_reader_serves_appends_but_refuses_truncation_and_replacement() {
        let root = snapshot_root("growing");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();

        use std::io::Write;

        let appended = root.join("appended.jsonl");
        fs::write(&appended, b"first\n").unwrap();
        let before = fs::symlink_metadata(&appended).unwrap();
        let body = read_growing_regular_file_from_meta_with(&appended, 1024, before, || {
            fs::OpenOptions::new()
                .append(true)
                .open(&appended)
                .unwrap()
                .write_all(b"second\n")
                .unwrap();
        })
        .unwrap();
        assert_eq!(body, b"first\n");

        let truncated = root.join("truncated.jsonl");
        fs::write(&truncated, b"first\nsecond\n").unwrap();
        let before = fs::symlink_metadata(&truncated).unwrap();
        let result = read_growing_regular_file_from_meta_with(&truncated, 1024, before, || {
            fs::write(&truncated, b"x").unwrap();
        });
        assert!(result.is_err());

        let replaced = root.join("replaced.jsonl");
        let moved = root.join("replaced-old.jsonl");
        fs::write(&replaced, b"first\n").unwrap();
        let before = fs::symlink_metadata(&replaced).unwrap();
        let result = read_growing_regular_file_from_meta_with(&replaced, 1024, before, || {
            fs::rename(&replaced, &moved).unwrap();
            fs::write(&replaced, b"first\nsecond\n").unwrap();
        });
        assert!(result.is_err());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn edge_snapshot_reads_only_the_requested_ends() {
        let root = snapshot_root("edges");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let path = root.join("derived.jsonl");
        fs::write(&path, b"abcdefghijklmnop").unwrap();
        let snapshot = regular_file_edge_snapshot(&path, 4).unwrap().unwrap();
        assert_eq!(snapshot.len, 16);
        assert_eq!(snapshot.head, b"abcd");
        assert_eq!(snapshot.tail, b"mnop");
        #[cfg(windows)]
        assert_eq!(
            snapshot.change_token,
            metadata_change_token(&path, &fs::symlink_metadata(&path).unwrap()).unwrap()
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_edge_token_uses_the_opened_file_handle() {
        let root = snapshot_root("windows-edge-handle");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let path = root.join("derived.jsonl");
        let old_path = root.join("derived-old.jsonl");
        fs::write(&path, b"source-A").unwrap();
        let before = fs::symlink_metadata(&path).unwrap();
        let file = open_regular_snapshot(&path, &before).unwrap();
        let opened = file.metadata().unwrap();
        fs::rename(&path, &old_path).unwrap();
        fs::write(&path, b"source-B").unwrap();

        let observed = open_file_change_token(&path, &file, &opened).unwrap();
        let old_token =
            metadata_change_token(&old_path, &fs::symlink_metadata(&old_path).unwrap()).unwrap();
        let replacement_token =
            metadata_change_token(&path, &fs::symlink_metadata(&path).unwrap()).unwrap();
        assert_eq!(observed, old_token);
        assert_ne!(observed, replacement_token);
        let (forced_hash, _) = windows_open_file_change_token(&file, &opened, true, false).unwrap();
        assert!(matches!(forced_hash, ChangeToken::ContentSha256(_)));
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(not(windows))]
    #[test]
    fn source_walk_skips_only_exact_private_sqlite_snapshot_directories() {
        let root = snapshot_root("sqlite-private-dirs");
        let _ = fs::remove_dir_all(&root);
        let hidden = root.join(".agrep-sqlite-123-abcd-0");
        let near_prefix = root.join(".agrep-sqlite-not-ours");
        let near_suffix = root.join(".agrep-sqlite-123-abcd-0-extra");
        for directory in [&hidden, &near_prefix, &near_suffix] {
            fs::create_dir_all(directory).unwrap();
            fs::write(directory.join("snapshot.db"), b"db").unwrap();
        }
        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let snapshot = adapter_source(&adapter).unwrap();
        assert!(!snapshot
            .files
            .iter()
            .any(|file| file.path.starts_with(&hidden)));
        assert!(snapshot
            .files
            .iter()
            .any(|file| file.path.starts_with(&near_prefix)));
        assert!(snapshot
            .files
            .iter()
            .any(|file| file.path.starts_with(&near_suffix)));
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn sqlite_source_token_ignores_hardlink_only_ctime_changes() {
        let root = snapshot_root("sqlite-hardlink-token");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let path = root.join("state.db");
        fs::write(&path, vec![0x5a; 16 * 1024]).unwrap();
        let before = source_file_from_meta(
            "snapshot-test",
            &path,
            false,
            true,
            fs::symlink_metadata(&path).unwrap(),
        )
        .unwrap();
        let alias = root.join("alias.db");
        fs::hard_link(&path, &alias).unwrap();
        fs::remove_file(alias).unwrap();
        let after = source_file_from_meta(
            "snapshot-test",
            &path,
            false,
            true,
            fs::symlink_metadata(&path).unwrap(),
        )
        .unwrap();
        assert_eq!(before, after);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn change_tokens_keep_full_content_identity_in_keys() {
        let digest = [0xabu8; 32];
        let metadata = ChangeToken::Metadata(0x1020_3040_5060_7080);
        let content = ChangeToken::ContentSha256(digest);
        let mut metadata_key = Vec::new();
        let mut content_key = Vec::new();

        metadata.append_key(&mut metadata_key);
        content.append_key(&mut content_key);

        assert_eq!(metadata_key.len(), 9);
        assert_eq!(content_key, [&[1_u8][..], &digest].concat());
    }

    #[test]
    fn source_snapshot_is_sorted_and_detects_add_remove() {
        let root = snapshot_root("files");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("z.jsonl"), "z").unwrap();
        fs::write(root.join("a.jsonl"), "a").unwrap();
        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let before = adapter_source(&adapter).unwrap();
        assert_eq!(before, adapter_source(&adapter).unwrap());
        assert!(before.files.windows(2).all(|w| w[0] <= w[1]));

        fs::write(root.join("new.jsonl"), "new").unwrap();
        let added = adapter_source(&adapter).unwrap();
        assert_ne!(before, added);
        fs::remove_file(root.join("new.jsonl")).unwrap();
        assert_eq!(before, adapter_source(&adapter).unwrap());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn older_source_snapshot_cannot_supply_preflight_stamps() {
        let snapshot = SourceSnapshot {
            snapshot_version: SOURCE_SNAPSHOT_VERSION - 1,
            cache_version: crate::ingest_cache::CACHE_VERSION,
            selection: "all".into(),
            adapters: Vec::new(),
            complete: true,
        };
        let encoded = bincode::serialize(&snapshot).unwrap();
        let view = source_snapshot_view(&encoded).unwrap();
        assert!(!view.complete());
        assert!(view.stat_stamps().is_empty());
        assert!(!source_snapshot_complete(&encoded));
    }

    #[cfg(windows)]
    #[test]
    fn version_eight_source_snapshot_wire_fails_closed() {
        #[derive(Serialize)]
        struct LegacyFile {
            agent: String,
            path: Vec<u16>,
            len: u64,
            mtime_secs: u64,
            mtime_nanos: u32,
            change_token: ChangeToken,
            content_hash: Option<u64>,
        }
        #[derive(Serialize)]
        struct LegacyAdapter {
            agent: String,
            files: Vec<LegacyFile>,
            tokens: Vec<(String, String)>,
            complete: bool,
        }
        #[derive(Serialize)]
        struct LegacySnapshot {
            snapshot_version: u32,
            cache_version: u32,
            selection: String,
            adapters: Vec<LegacyAdapter>,
            complete: bool,
        }
        let encoded = bincode::serialize(&LegacySnapshot {
            snapshot_version: 8,
            cache_version: crate::ingest_cache::CACHE_VERSION,
            selection: "all".into(),
            adapters: vec![LegacyAdapter {
                agent: "claude".into(),
                files: vec![LegacyFile {
                    agent: "claude".into(),
                    path: vec![b'C' as u16, b':' as u16, b'\\' as u16],
                    len: 1,
                    mtime_secs: 1,
                    mtime_nanos: 0,
                    change_token: ChangeToken::Metadata(1),
                    content_hash: None,
                }],
                tokens: Vec::new(),
                complete: true,
            }],
            complete: true,
        })
        .unwrap();
        assert!(!source_snapshot_complete(&encoded));
    }

    #[cfg(any(unix, windows))]
    #[test]
    fn source_snapshot_detects_same_size_edit_with_restored_mtime() {
        let root = snapshot_root("restored-mtime");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        fs::write(&source, b"before").unwrap();
        let modified = fs::metadata(&source).unwrap().modified().unwrap();
        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let before = adapter_source(&adapter).unwrap();

        fs::write(&source, b"after!").unwrap();
        let file = fs::OpenOptions::new().write(true).open(&source).unwrap();
        file.set_times(fs::FileTimes::new().set_modified(modified))
            .unwrap();
        let after = adapter_source(&adapter).unwrap();

        assert_eq!(before.files[0].len, after.files[0].len);
        assert_eq!(before.files[0].mtime_secs, after.files[0].mtime_secs);
        assert_eq!(before.files[0].mtime_nanos, after.files[0].mtime_nanos);
        assert_ne!(before.files[0].change_token, after.files[0].change_token);
        assert_ne!(before, after);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn source_snapshot_walks_beyond_the_old_depth_limit() {
        let root = snapshot_root("deep-files");
        let _ = fs::remove_dir_all(&root);
        let mut dir = root.clone();
        for depth in 0..12 {
            dir.push(format!("level-{depth}"));
        }
        fs::create_dir_all(&dir).unwrap();
        let transcript = dir.join("rollout-deep.jsonl");
        fs::write(&transcript, "deep").unwrap();

        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let snapshot = adapter_source(&adapter).unwrap();
        assert!(snapshot.files.iter().any(|file| file.path == transcript));
        let census = store_census(&adapter);
        assert!(census.files.iter().any(|(path, _)| path == &transcript));
        let _ = fs::remove_dir_all(&root);
    }

    #[cfg(unix)]
    #[test]
    fn source_snapshot_rejects_symlinked_directories_and_files() {
        use std::os::unix::fs::symlink;

        let root = snapshot_root("symlink-cycle");
        let _ = fs::remove_dir_all(&root);
        let nested = root.join("nested");
        fs::create_dir_all(&nested).unwrap();
        fs::write(nested.join("session.jsonl"), "one").unwrap();
        symlink(&root, nested.join("back-to-root")).unwrap();
        let outside = snapshot_root("symlink-outside");
        let _ = fs::remove_dir_all(&outside);
        fs::create_dir_all(outside.join("hidden-dir")).unwrap();
        let outside_file = outside.join("outside.jsonl");
        fs::write(&outside_file, "outside").unwrap();
        symlink(&outside, root.join("outside-dir")).unwrap();
        symlink(&outside_file, root.join("outside-file.jsonl")).unwrap();

        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let snapshot = adapter_source(&adapter).unwrap();
        assert_eq!(snapshot.files.len(), 1);
        assert!(!snapshot.complete);
        assert_eq!(snapshot.issues.len(), 3);
        assert!(snapshot
            .issues
            .iter()
            .all(|issue| issue.kind() == "unsupported-link"));
        assert!(snapshot
            .issues
            .iter()
            .any(|issue| issue.path() == nested.join("back-to-root").to_string_lossy()));
        assert!(snapshot
            .issues
            .iter()
            .any(|issue| issue.path() == root.join("outside-dir").to_string_lossy()));
        assert!(snapshot
            .issues
            .iter()
            .any(|issue| issue.path() == root.join("outside-file.jsonl").to_string_lossy()));
        let census = store_census(&adapter);
        assert_eq!(census.files.len(), 1);
        assert_eq!(census.issues, snapshot.issues);
        assert_eq!(count_subdirs(&root), 1);
        assert_eq!(count_subdirs(&root.join("outside-dir")), 0);
        let _ = fs::remove_dir_all(&root);
        let _ = fs::remove_dir_all(&outside);
    }

    #[cfg(unix)]
    #[test]
    fn source_snapshot_reports_link_and_special_roots_as_incomplete() {
        use std::os::unix::fs::symlink;

        let outside = snapshot_root("root-link-outside");
        let linked = snapshot_root("root-link");
        for path in [&outside, &linked] {
            let _ = fs::remove_file(path);
            let _ = fs::remove_dir_all(path);
        }
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("session.jsonl"), "outside").unwrap();
        symlink(&outside, &linked).unwrap();
        let link_adapter = SnapshotAdapter {
            root: linked.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let link_snapshot = adapter_source(&link_adapter).unwrap();
        assert!(!link_snapshot.complete);
        assert!(link_snapshot.files.is_empty());
        assert_eq!(link_snapshot.issues[0].kind(), "unsupported-link");

        let special_adapter = SnapshotAdapter {
            root: PathBuf::from("/dev/null"),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let special_snapshot = adapter_source(&special_adapter).unwrap();
        assert!(!special_snapshot.complete);
        assert!(special_snapshot.files.is_empty());
        assert_eq!(special_snapshot.issues[0].kind(), "unsupported-file-type");

        fs::remove_file(&linked).unwrap();
        let _ = fs::remove_dir_all(&outside);
    }

    #[cfg(windows)]
    #[test]
    fn source_snapshot_rejects_windows_junctions() {
        let root = snapshot_root("junction-root");
        let outside = snapshot_root("junction-outside");
        let _ = fs::remove_dir_all(&root);
        let _ = fs::remove_dir_all(&outside);
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::write(root.join("session.jsonl"), "inside").unwrap();
        fs::write(outside.join("outside.jsonl"), "outside").unwrap();
        let junction = root.join("outside-junction");
        let result = std::process::Command::new("cmd")
            .args(["/d", "/c", "mklink", "/J"])
            .arg(&junction)
            .arg(&outside)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let junction_meta = fs::symlink_metadata(&junction).unwrap();
        assert!(metadata_is_link(&junction_meta));
        assert!(plain_metadata(&junction).unwrap().is_none());
        let junction_entry = fs::read_dir(&root)
            .unwrap()
            .map(Result::unwrap)
            .find(|entry| entry.path() == junction)
            .unwrap();
        assert!(metadata_is_link(&junction_entry.metadata().unwrap()));
        assert!(plain_entry_metadata(&junction_entry).unwrap().is_none());

        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let snapshot = adapter_source(&adapter).unwrap();
        assert_eq!(snapshot.files.len(), 1);
        assert_eq!(snapshot.files[0].path, root.join("session.jsonl"));
        assert_eq!(store_census(&adapter).files.len(), 1);

        fs::remove_dir(&junction).unwrap();
        let _ = fs::remove_dir_all(&root);
        let _ = fs::remove_dir_all(&outside);
    }

    #[cfg(unix)]
    #[test]
    fn source_snapshot_skips_unreadable_subtree_without_blessing_it() {
        use std::os::unix::fs::PermissionsExt;

        let root = snapshot_root("unreadable-subtree");
        let _ = fs::remove_dir_all(&root);
        let blocked = root.join("blocked");
        fs::create_dir_all(&blocked).unwrap();
        let readable = root.join("readable.jsonl");
        fs::write(&readable, "readable").unwrap();
        fs::write(blocked.join("hidden.jsonl"), "hidden").unwrap();
        fs::set_permissions(&blocked, fs::Permissions::from_mode(0o000)).unwrap();

        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let snapshot = adapter_source(&adapter).unwrap();
        assert!(!snapshot.complete);
        assert_eq!(snapshot.files.len(), 1);
        assert_eq!(snapshot.files[0].path, readable);
        assert_eq!(snapshot.issues.len(), 1);
        assert_eq!(snapshot.issues[0].agent(), "snapshot-test");
        assert_eq!(snapshot.issues[0].path(), blocked.to_string_lossy());
        assert_eq!(snapshot.issues[0].kind(), "permission-denied");

        let diagnostic = store_diagnostic(&adapter).unwrap();
        assert_eq!(diagnostic.files(), 1);
        assert_eq!(diagnostic.state(), "source-unreadable");
        assert_eq!(diagnostic.issues(), snapshot.issues.as_slice());

        let encoded = bincode::serialize(&SourceSnapshot {
            snapshot_version: SOURCE_SNAPSHOT_VERSION,
            cache_version: crate::ingest_cache::CACHE_VERSION,
            selection: "snapshot".to_string(),
            adapters: vec![snapshot],
            complete: false,
        })
        .unwrap();
        assert_eq!(source_snapshot_issues(&encoded), diagnostic.issues());

        fs::set_permissions(&blocked, fs::Permissions::from_mode(0o700)).unwrap();
        let _ = fs::remove_dir_all(&root);
    }

    #[cfg(unix)]
    #[test]
    fn unreadable_root_is_reported_instead_of_filtered_as_absent() {
        use std::os::unix::fs::PermissionsExt;

        let root = snapshot_root("unreadable-root");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("hidden.jsonl"), "hidden").unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o000)).unwrap();

        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let snapshot = adapter_source(&adapter).unwrap();
        assert!(!snapshot.complete);
        assert!(snapshot.files.is_empty());
        assert_eq!(snapshot.issues[0].path(), root.to_string_lossy());
        assert_eq!(snapshot.issues[0].kind(), "permission-denied");
        let diagnostic = store_diagnostic(&adapter).unwrap();
        assert_eq!(diagnostic.files(), 0);
        assert_eq!(diagnostic.state(), "source-unreadable");

        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn absent_store_is_not_an_unreadable_store() {
        let root = snapshot_root("absent-store");
        let _ = fs::remove_dir_all(&root);
        let adapter = SnapshotAdapter {
            root,
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let snapshot = adapter_source(&adapter).unwrap();
        assert!(snapshot.complete);
        assert!(snapshot.files.is_empty());
        assert!(snapshot.issues.is_empty());
        assert!(store_diagnostic(&adapter).is_none());
    }

    #[test]
    fn adapter_freshness_filters_match_discovered_sources() {
        let claude = crate::ingest::claude::Claude;
        let claude_project = PathBuf::from("project");
        assert!(claude.freshness_content(&claude_project.join("session.jsonl")));
        assert!(!claude.freshness_content(Path::new("session.jsonl")));
        assert!(claude.freshness_content(
            &claude_project
                .join("Temp-claude-work")
                .join("session.jsonl")
        ));
        assert!(claude.freshness_content(
            &claude_project
                .join("nested-claude-worker")
                .join("session.jsonl")
        ));
        assert!(claude.freshness_content(
            &claude_project
                .join("C--Users-Example-AppData-Local-Temp-claude-worker--zgq26av")
                .join("session.jsonl")
        ));
        assert!(
            !claude.freshness_content(&claude_project.join("a/b/c/d/e/f").join("session.jsonl"))
        );
        assert!(!claude.freshness_content(&claude_project.join("session.json")));

        let codex = crate::ingest::codex::Codex;
        assert!(codex.freshness_content(Path::new("2026/07/20/rollout-session.jsonl")));
        assert!(!codex.freshness_content(Path::new("2026/07/20/metadata.jsonl")));

        let opencode = crate::ingest::opencode::Opencode;
        assert!(opencode.freshness_content(Path::new("opencode.db")));
        assert!(opencode.freshness_content(Path::new("opencode-custom-copy.db")));
        for ignored in [
            "opencode.db-wal",
            "opencode.db-shm",
            "opencode.db.bak",
            "opencode.db.corrupted",
            "unrecognized.db",
        ] {
            assert!(!opencode.freshness_content(Path::new(ignored)), "{ignored}");
        }
    }

    #[test]
    fn always_snapshot_hashes_same_size_content() {
        let root = snapshot_root("always");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let path = root.join("session.jsonl");
        fs::write(&path, "old!").unwrap();
        let always = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Always,
            tokens: TokenAvailability::Empty,
        };
        let before = adapter_source(&always).unwrap();
        fs::write(&path, "NEW!").unwrap(); // same byte length; hash, not size, proves the edit
        let mut after = adapter_source(&always).unwrap();
        assert_ne!(before.files[0].content_hash, after.files[0].content_hash);
        // Even if an external writer restored the old mtime, the content hash keeps these
        // snapshots distinct. Normalize metadata in the test to exercise that exact edge.
        after.files[0].mtime_secs = before.files[0].mtime_secs;
        after.files[0].mtime_nanos = before.files[0].mtime_nanos;
        assert_ne!(before, after);

        let stat = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        assert_eq!(adapter_source(&stat).unwrap().files[0].content_hash, None);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn sqlite_snapshots_track_wal_without_weakening_token_requirements() {
        let root = snapshot_root("token").with_extension("db");
        let _ = fs::remove_file(&root);
        let _ = fs::remove_file(crate::ingest::sqlite_sidecar(&root, "-wal"));
        fs::write(&root, "db").unwrap();
        let unavailable = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Token,
            tokens: TokenAvailability::Unreadable(vec![TokenReadIssue::new(
                &root,
                "fixture unavailable",
            )]),
        };
        let unavailable = adapter_source(&unavailable).unwrap();
        assert!(!unavailable.complete);
        assert!(unavailable.tokens.is_empty());
        assert_eq!(unavailable.issues.len(), 1);
        assert_eq!(unavailable.issues[0].kind(), "source-unreadable");
        assert_eq!(unavailable.issues[0].path(), root.to_string_lossy());
        assert_eq!(unavailable.issues[0].reason(), "fixture unavailable");

        let adapter = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Token,
            tokens: TokenAvailability::Data(vec![("s1".into(), "t1".into())]),
        };
        let before = adapter_source(&adapter).unwrap();
        fs::write(crate::ingest::sqlite_sidecar(&root, "-wal"), "wal").unwrap();
        assert_eq!(before, adapter_source(&adapter).unwrap());

        let stat = SnapshotAdapter {
            root: root.clone(),
            kind: Fingerprint::Stat,
            tokens: TokenAvailability::Empty,
        };
        let after = adapter_source(&stat).unwrap();
        let _ = fs::remove_file(crate::ingest::sqlite_sidecar(&root, "-wal"));
        let without_wal = adapter_source(&stat).unwrap();
        assert_ne!(after, without_wal);
        assert!(after
            .files
            .iter()
            .any(|f| f.path == crate::ingest::sqlite_sidecar(&root, "-wal")));
        let _ = fs::remove_file(&root);
    }

    // An edited conversation with the same message count and last id must change the
    // token - the exact case count+last-id fingerprinting misses.
    #[test]
    fn token_edited_body_same_count_changes_hash() {
        let before = serde_json::json!({
            "messages": [{"id": "m1", "text": "old"}, {"id": "m2", "text": "reply"}]
        });
        let after = serde_json::json!({
            "messages": [{"id": "m1", "text": "EDITED"}, {"id": "m2", "text": "reply"}]
        });
        assert_ne!(token_fingerprint(&before), token_fingerprint(&after));
    }

    // Detection probes: a fake store under AGREP_HOME is detected with the right count; an
    // absent one is not. Serialized (they share the AGREP_HOME process env).
    #[test]
    fn detection_probes() {
        use std::sync::Mutex;
        static ENV_LOCK: Mutex<()> = Mutex::new(());
        let _g = ENV_LOCK.lock().unwrap();

        let root = std::env::temp_dir().join(format!("agrep-detect-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        // copilot: two session dirs; qwen: one chats/*.json
        std::fs::create_dir_all(root.join(".copilot").join("session-state").join("s1")).unwrap();
        std::fs::create_dir_all(root.join(".copilot").join("session-state").join("s2")).unwrap();
        std::fs::create_dir_all(root.join(".qwen").join("tmp").join("h1").join("chats")).unwrap();
        std::fs::write(
            root.join(".qwen")
                .join("tmp")
                .join("h1")
                .join("chats")
                .join("session-x.json"),
            "{}",
        )
        .unwrap();

        let prev = std::env::var_os("AGREP_HOME");
        std::env::set_var("AGREP_HOME", &root);
        let got: std::collections::HashMap<&str, usize> = detected().into_iter().collect();
        match prev {
            Some(v) => std::env::set_var("AGREP_HOME", v),
            None => std::env::remove_var("AGREP_HOME"),
        }
        let _ = std::fs::remove_dir_all(&root);

        assert_eq!(got.get("copilot"), Some(&2));
        assert_eq!(got.get("qwen"), Some(&1));
    }

    #[test]
    fn token_prefers_updated_at() {
        let a = serde_json::json!({"updatedAt": "2026-01-02T10:00:00Z", "messages": [{"x": 1}]});
        let b = serde_json::json!({"updatedAt": "2026-01-02T10:00:00Z", "messages": [{"x": 999}]});
        // same updatedAt -> same token even though the body differs (the store's own signal wins)
        assert_eq!(token_fingerprint(&a), token_fingerprint(&b));
        assert!(token_fingerprint(&a).starts_with("u:"));
        // no updatedAt -> falls back to the content hash
        let c = serde_json::json!({"messages": [{"x": 1}]});
        assert!(token_fingerprint(&c).starts_with("h:"));
    }

    #[test]
    fn cleanly_empty_token_store_agents_need_token_kind_and_a_clean_observation() {
        let present_db = SourceFile {
            agent: "cursor".into(),
            path: PathBuf::from("state.vscdb"),
            len: 4096,
            mtime_secs: 1,
            mtime_nanos: 0,
            change_token: ChangeToken::Metadata(1),
            #[cfg(windows)]
            file_identity: None,
            content_hash: None,
        };
        let adapter = |agent: &str, files: Vec<SourceFile>, issues, complete| AdapterSource {
            agent: agent.into(),
            files,
            tokens: Vec::new(),
            issues,
            complete,
        };
        let snapshot = SourceSnapshot {
            snapshot_version: SOURCE_SNAPSHOT_VERSION,
            cache_version: crate::ingest_cache::CACHE_VERSION,
            selection: "all".into(),
            adapters: vec![
                // present-but-empty token store: the validly-empty deletion observation
                adapter("cursor", vec![present_db], Vec::new(), true),
                // zero tokens is normal for a Stat adapter, never a deletion observation
                adapter("claude", Vec::new(), Vec::new(), true),
                // an unreadable token store is a torn read, not a clean empty
                adapter(
                    "crush",
                    Vec::new(),
                    vec![SourceIssue::new(
                        "crush",
                        Path::new("crush.db"),
                        "source-unreadable",
                        "cannot open",
                    )],
                    false,
                ),
            ],
            complete: true,
        };
        let encoded = bincode::serialize(&snapshot).unwrap();
        let view = source_snapshot_view(&encoded).unwrap();
        let empty = view.cleanly_empty_token_store_agents();
        assert!(empty.contains("cursor"));
        assert!(!empty.contains("claude"));
        assert!(!empty.contains("crush"));
        // the ENOENT set is unchanged: a present store is not absent
        assert!(!view.cleanly_absent_agents().contains("cursor"));
    }
}
