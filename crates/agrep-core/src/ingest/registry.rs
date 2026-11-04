//! The one registration point for adapters. Adding an agent is a trait impl in its module
//! plus a single line in `ADAPTERS` here.
//!
//! The driver runs cache-driven adapters sequentially sharing the per-file parse cache; the
//! full-parse (Always) adapters run in a sibling rayon task; results are concatenated and
//! deduped by message content/position or event call id. Sessions with colliding source turns
//! receive deterministic unique turn numbers before publication.

use std::fs;
use std::io::{ErrorKind, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::{
    collections::HashMap,
    sync::{OnceLock, RwLock},
};

use serde::{Deserialize, Serialize};

use crate::intake_batch::IntakeBatch;
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

/// Issue kind of an attribution source (cursor's workspace metadata) the census could not read
/// while it still listed every conversation: nothing is hidden or lost, some projects are
/// provisional. The publication guard admits it without a material verdict for its path.
pub const ATTRIBUTION_ISSUE_KIND: &str = "attribution-unreadable";

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TokenAvailability {
    Data(Vec<(String, String)>),
    Empty,
    /// Every database the census opened lists its conversations; `issues` name each it could not.
    Partial {
        tokens: Vec<(String, String)>,
        issues: Vec<TokenReadIssue>,
    },
    Unreadable(Vec<TokenReadIssue>),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TokenReadIssue {
    pub path: PathBuf,
    pub kind: &'static str,
    pub reason: String,
    /// Conversations listed under a fallback project because `path`, an attribution source,
    /// could not be read. Empty when the issue hides conversations instead.
    pub provisional: Vec<String>,
    /// Conversations of a listed store this issue leaves unlisted; empty when `path` itself is
    /// the store (or a registry) that stayed unlisted whole.
    pub hidden: Vec<String>,
}

impl TokenReadIssue {
    /// A census failure a retry may clear; the publication guard keeps its fail-closed retry.
    pub fn new(path: impl Into<PathBuf>, reason: impl Into<String>) -> Self {
        Self::with_kind(path, "source-unreadable", reason)
    }

    /// `kind` is the snapshot issue kind, so an adapter can name a durable defect as one.
    pub fn with_kind(
        path: impl Into<PathBuf>,
        kind: &'static str,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            path: path.into(),
            kind,
            reason: reason.into(),
            provisional: Vec::new(),
            hidden: Vec::new(),
        }
    }

    pub fn provisional(mut self, sessions: Vec<String>) -> Self {
        self.provisional = sessions;
        self
    }

    pub fn hiding(mut self, sessions: Vec<String>) -> Self {
        self.hidden = sessions;
        self
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

    /// A census over several databases: the conversations of each it opened beside the issue
    /// naming each it could not. Nothing listed is plain `Unreadable`.
    pub fn from_observation(tokens: Vec<(String, String)>, issues: Vec<TokenReadIssue>) -> Self {
        if issues.is_empty() {
            Self::from_tokens(tokens)
        } else if tokens.is_empty() {
            Self::Unreadable(issues)
        } else {
            Self::Partial { tokens, issues }
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
    fn collect(&self, cache: &mut IntakeBatch) -> (Vec<Message>, Vec<Event>);
    fn collect_checked(
        &self,
        cache: &mut IntakeBatch,
    ) -> (
        Vec<Message>,
        Vec<Event>,
        crate::intake_batch::ReadOutcome,
        Vec<crate::intake_batch::SourceReadIssue>,
    ) {
        let (messages, events) = self.collect(cache);
        (
            messages,
            events,
            crate::intake_batch::ReadOutcome::Complete,
            Vec::new(),
        )
    }
    /// Whether a read of one of this adapter's sources can come back `ReadOutcome::Partial`.
    /// Cache generations older than the current one published such reads without caching them.
    fn may_read_partially(&self) -> bool {
        false
    }
    /// Whether a failed read of `path` costs a whole-store read exactly the rows of the units
    /// beneath it. A path every unit draws on (an index or registry) may change what the rest
    /// yield, so the default claims nothing.
    fn unit_scoped_read_issue(&self, _path: &std::path::Path) -> bool {
        false
    }
    /// Where this adapter's store lives (dirs or files; absent candidates are fine).
    /// The doctor freshness canary stats these: store activity the parser never turns
    /// into messages is adapter drift, and this is what makes it visible.
    fn store_roots(&self) -> Vec<std::path::PathBuf>;
    /// Native recovery inputs. This belongs to the adapter registry so archive
    /// and intake never maintain competing source-location tables.
    fn archive_roots(&self) -> Vec<PathBuf> {
        self.freshness_roots()
    }
    fn archive_content(&self, path: &Path) -> bool {
        self.freshness_content(path)
    }
    /// Is this file conversation CONTENT the adapter would parse? The canary counts
    /// only these - state/config files update on app boots without any new sessions,
    /// and a canary that fires on those trains everyone to ignore it.
    fn store_content(&self, _path: &std::path::Path) -> bool {
        true
    }
    /// The id the store's own filename gives `session`'s transcript at `path`, when it differs
    /// from the header id the rows carry; publication resolves both to one family.
    fn session_alias(&self, _path: &Path, _session: &str) -> Option<String> {
        None
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
    /// [`Self::intake_tokens`] beside the parse cache the diagnostic decoded (None when it could
    /// not): an adapter whose tokens depend on what the last generation published (cursor's
    /// retained attribution) reads it rather than the live sources alone.
    fn intake_tokens_with(
        &self,
        _cache: Option<&crate::intake_batch::IntakeBatch>,
    ) -> TokenAvailability {
        self.intake_tokens()
    }
    /// For `Fingerprint::Token` stores: the prefix every freshness token id and parse-cache id of
    /// `database`'s conversations starts with. None keeps a listed database counted as material.
    fn token_prefix(&self, _database: &Path) -> Option<String> {
        None
    }
    /// The inverse of [`Self::token_prefix`]: the database part of a parse-cache id, equal to
    /// `token_prefix` of the database that conversation came from. "" spans every database.
    fn token_namespace<'a>(&self, _cache_id: &'a str) -> &'a str {
        ""
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

/// [`Adapter::session_alias`] for the adapter that owns `agent`'s rows.
pub fn session_alias(agent: &str, path: &Path, session: &str) -> Option<String> {
    ADAPTERS
        .iter()
        .find(|adapter| adapter.name() == agent)
        .and_then(|adapter| adapter.session_alias(path, session))
}

/// [`Adapter::token_prefix`] of `agent`'s store, when it is a `Fingerprint::Token` adapter.
pub fn token_prefix(agent: &str, database: &Path) -> Option<String> {
    ADAPTERS
        .iter()
        .find(|adapter| adapter.name() == agent && adapter.fingerprint() == Fingerprint::Token)
        .and_then(|adapter| adapter.token_prefix(database))
}

/// [`Adapter::token_namespace`] of a cache id of `agent`; "" (every database) when no token
/// adapter claims the agent.
pub fn token_namespace(agent: &str, cache_id: &str) -> String {
    ADAPTERS
        .iter()
        .find(|adapter| adapter.name() == agent && adapter.fingerprint() == Fingerprint::Token)
        .map(|adapter| adapter.token_namespace(cache_id).to_owned())
        .unwrap_or_default()
}

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

/// Linux stamps ctime from the coarse tick clock, so a change in the same tick as the
/// last one keeps its token. Tests that need a visible change wait for the next tick.
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
    windows_metadata_identity(path, metadata, false, false).map(|(token, _)| token)
}

#[cfg(windows)]
pub fn metadata_change_token_with_file_identity(
    path: &Path,
    metadata: &fs::Metadata,
) -> std::io::Result<(ChangeToken, WindowsFileIdentity)> {
    let (token, identity) = windows_metadata_identity(path, metadata, true, false)?;
    let identity = identity.ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "filesystem did not provide a stable file identity",
        )
    })?;
    Ok((token, identity))
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

#[cfg(windows)]
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
fn windows_usn_change_token(usn: i64) -> Option<ChangeToken> {
    if usn == 0 {
        None
    } else {
        Some(ChangeToken::Metadata(
            (usn as u64).rotate_left(7) ^ 0x5553_4e5f_4649_4c45,
        ))
    }
}

#[cfg(windows)]
fn windows_open_file_change_token(
    file: &fs::File,
    metadata: &fs::Metadata,
    force_content_hash: bool,
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
            if let Some(token) = windows_usn_change_token(usn) {
                return Ok((token, initial_identity));
            }
        }
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
    let (token, initial_identity) =
        windows_open_file_change_token(&file, metadata, force_content_hash)?;
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

/// Whether a failed `symlink_metadata(path)` proves nothing exists at `path`: ENOENT, or a
/// parent that is not a directory (or a name too long) where the deepest ancestor that stats is
/// not a link. A link there is mispointed and, like a loop (ELOOP), hides data that may be
/// intact; EACCES and EIO likewise stay unavailable and retain their rows.
pub fn stat_proves_absent(path: &Path, error: &std::io::Error) -> bool {
    match error.kind() {
        ErrorKind::NotFound => true,
        ErrorKind::NotADirectory | ErrorKind::InvalidFilename => path
            .ancestors()
            .skip(1)
            .find_map(|ancestor| fs::symlink_metadata(ancestor).ok())
            .is_some_and(|metadata| !metadata_is_link(&metadata)),
        _ => false,
    }
}

/// Read-open flags that refuse a final-component symlink (ELOOP) and never block on a FIFO.
/// The O_* values differ by architecture: x86_64's O_NOFOLLOW is O_LARGEFILE on aarch64 Linux.
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub fn nofollow_read_options(options: &mut fs::OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;

    options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub(crate) fn secure_read_options(options: &mut fs::OpenOptions) {
    nofollow_read_options(options);
}

#[cfg(windows)]
pub(crate) fn secure_read_options(options: &mut fs::OpenOptions) {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    options
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
pub(crate) fn secure_read_options(_options: &mut fs::OpenOptions) {}

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

pub(crate) fn open_regular_snapshot(
    path: &Path,
    before: &fs::Metadata,
) -> std::io::Result<fs::File> {
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RegularFileEdgeSnapshot {
    pub len: u64,
    pub modified: std::time::SystemTime,
    pub change_token: ChangeToken,
    pub head: Vec<u8>,
    pub tail: Vec<u8>,
}

#[cfg(windows)]
fn open_file_change_token(
    _path: &Path,
    file: &fs::File,
    metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    windows_open_file_change_token(file, metadata, false).map(|(token, _)| token)
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

/// normally agrep's own: its target is indexed directly (an alias) or was since removed.
pub enum StoreLink {
    /// a regular transcript inside the store, reached by this walked path
    Alias(PathBuf),
    /// a transcript path inside the store with nothing there any more
    Dangling,
    /// anything else: outside the store, not a transcript, a loop, a directory
    Foreign,
}

fn lexical_path(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

pub fn classify_store_link(link: &Path, root: &Path, accepts: &dyn Fn(&Path) -> bool) -> StoreLink {
    if !accepts(link) {
        return StoreLink::Foreign;
    }
    let Ok(raw) = fs::read_link(link) else {
        return StoreLink::Foreign;
    };
    let lexical = if raw.is_absolute() {
        raw
    } else {
        link.parent()
            .map_or_else(|| raw.clone(), |parent| parent.join(&raw))
    };
    if !accepts(&lexical) {
        return StoreLink::Foreign;
    }
    let canonical_root = fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    match fs::canonicalize(&lexical) {
        Ok(target) => {
            if !fs::symlink_metadata(&target).is_ok_and(|metadata| metadata.is_file()) {
                return StoreLink::Foreign;
            }
            match target.strip_prefix(&canonical_root) {
                Ok(relative) => StoreLink::Alias(root.join(relative)),
                Err(_) => StoreLink::Foreign,
            }
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {
            let target = lexical_path(&lexical);
            let inside = target.strip_prefix(lexical_path(root)).is_ok()
                || target.strip_prefix(&canonical_root).is_ok();
            if inside {
                StoreLink::Dangling
            } else {
                StoreLink::Foreign
            }
        }
        Err(_) => StoreLink::Foreign,
    }
}

pub(crate) fn sqlite_database(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| matches!(e, "db" | "sqlite" | "sqlite3" | "vscdb"))
}
