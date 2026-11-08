//! Windows file identity read from an open handle, and the change token (USN, else SHA-256 of
//! the content) a handle's file carries.

use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::os::windows::io::{AsRawHandle, RawHandle};
use std::path::Path;

use serde::{Deserialize, Serialize};

use super::{metadata_is_link, ChangeToken};

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct WindowsFileIdentity {
    pub(crate) kind: u8,
    pub(crate) volume: u64,
    pub(crate) id: [u8; 16],
}

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

pub fn metadata_change_token(path: &Path, metadata: &fs::Metadata) -> std::io::Result<ChangeToken> {
    windows_metadata_identity(path, metadata)
}

pub(crate) fn opened_file_identity(file: &fs::File) -> std::io::Result<WindowsFileIdentity> {
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
    let state = windows_handle_state(file.as_raw_handle())?;
    if state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "source is a reparse point",
        ));
    }
    Ok(state.identity)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct WindowsHandleState {
    pub(super) identity: WindowsFileIdentity,
    legacy_volume: u64,
    legacy_inode: u64,
    pub(super) size: u64,
    last_write: u64,
    change_time: u64,
    pub(super) attributes: u32,
}

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
fn same_open_file(left: &WindowsHandleState, right: &WindowsHandleState) -> bool {
    left.identity == right.identity
        && left.legacy_volume == right.legacy_volume
        && left.legacy_inode == right.legacy_inode
        && left.attributes == right.attributes
}

pub(super) fn windows_handle_state(handle: RawHandle) -> std::io::Result<WindowsHandleState> {
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut legacy = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(handle, &mut legacy) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let legacy_volume = u64::from(legacy.dwVolumeSerialNumber);
    let legacy_inode = (u64::from(legacy.nFileIndexHigh) << 32) | u64::from(legacy.nFileIndexLow);
    let identity = handle_file_identity(handle, &legacy);
    let basic = handle_basic_info(handle)?;
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

/// The 128-bit file id where the volume reports one, else the legacy 64-bit index.
fn handle_file_identity(
    handle: RawHandle,
    legacy: &windows_sys::Win32::Storage::FileSystem::BY_HANDLE_FILE_INFORMATION,
) -> WindowsFileIdentity {
    use windows_sys::Win32::Storage::FileSystem::{
        FileIdInfo, GetFileInformationByHandleEx, FILE_ID_INFO,
    };

    let mut file_id = FILE_ID_INFO::default();
    if unsafe {
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
    }
}

fn handle_basic_info(
    handle: RawHandle,
) -> std::io::Result<windows_sys::Win32::Storage::FileSystem::FILE_BASIC_INFO> {
    use windows_sys::Win32::Storage::FileSystem::{
        FileBasicInfo, GetFileInformationByHandleEx, FILE_BASIC_INFO,
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
    Ok(basic)
}

fn windows_usn_change_token(usn: i64) -> Option<ChangeToken> {
    if usn == 0 {
        None
    } else {
        Some(ChangeToken::Metadata(
            (usn as u64).rotate_left(7) ^ 0x5553_4e5f_4649_4c45,
        ))
    }
}

/// The file's USN token, else the SHA-256 of its content, beside the handle state it began at.
pub(super) fn windows_open_file_change_token(
    file: &fs::File,
    metadata: &fs::Metadata,
) -> std::io::Result<(ChangeToken, WindowsHandleState)> {
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;

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
    if let Some(token) = usn_change_token(file, &initial_identity)? {
        return Ok((token, initial_identity));
    }
    let digest = content_sha256(file)?;
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

/// The token of the file's USN record; None when the volume keeps no USN for it (the query
/// fails or the USN is zero), so the content hash decides instead.
fn usn_change_token(
    file: &fs::File,
    initial_identity: &WindowsHandleState,
) -> std::io::Result<Option<ChangeToken>> {
    use windows_sys::Win32::System::Ioctl::{FSCTL_READ_FILE_USN_DATA, READ_FILE_USN_DATA};
    use windows_sys::Win32::System::IO::DeviceIoControl;

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
    if ok == 0 {
        return Ok(None);
    }
    let usn = usn_from_record(&output, returned as usize)?;
    if !same_open_file(
        &windows_handle_state(file.as_raw_handle())?,
        initial_identity,
    ) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "source was replaced while reading its freshness identity",
        ));
    }
    Ok(windows_usn_change_token(usn))
}

/// The USN field of a version 2 or 3 USN record of `bytes` returned bytes.
fn usn_from_record(output: &[u8], bytes: usize) -> std::io::Result<i64> {
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
    Ok(i64::from_le_bytes(
        output[usn_offset..usn_offset + 8].try_into().unwrap(),
    ))
}

/// SHA-256 of the whole file from its start, through the system crypto provider.
fn content_sha256(file: &fs::File) -> std::io::Result<[u8; 32]> {
    use windows_sys::Win32::Security::Cryptography::{CryptDestroyHash, CryptReleaseContext};

    let mut provider = 0_usize;
    let mut hash = 0_usize;
    let result = hash_file_with(file, &mut provider, &mut hash);
    if hash != 0 {
        unsafe { CryptDestroyHash(hash) };
    }
    if provider != 0 {
        unsafe { CryptReleaseContext(provider, 0) };
    }
    result
}

/// Acquire a provider and hash into `provider`/`hash` (released by the caller), then hash the
/// file's content.
fn hash_file_with(
    file: &fs::File,
    provider: &mut usize,
    hash: &mut usize,
) -> std::io::Result<[u8; 32]> {
    use windows_sys::Win32::Security::Cryptography::{
        CryptAcquireContextW, CryptCreateHash, CALG_SHA_256, CRYPT_VERIFYCONTEXT, PROV_RSA_AES,
    };

    if unsafe {
        CryptAcquireContextW(
            provider,
            std::ptr::null(),
            std::ptr::null(),
            PROV_RSA_AES,
            CRYPT_VERIFYCONTEXT,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { CryptCreateHash(*provider, CALG_SHA_256, 0, 0, hash) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    hash_contents(*hash, file)?;
    hash_digest(*hash)
}

fn hash_contents(hash: usize, file: &fs::File) -> std::io::Result<()> {
    use windows_sys::Win32::Security::Cryptography::CryptHashData;

    let mut reader = file;
    reader.seek(SeekFrom::Start(0))?;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            return Ok(());
        }
        if unsafe { CryptHashData(hash, buffer.as_ptr(), count as u32, 0) } == 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
}

fn hash_digest(hash: usize) -> std::io::Result<[u8; 32]> {
    use windows_sys::Win32::Security::Cryptography::{CryptGetHashParam, HP_HASHVAL};

    let mut digest = [0_u8; 32];
    let mut digest_len = digest.len() as u32;
    if unsafe { CryptGetHashParam(hash, HP_HASHVAL, digest.as_mut_ptr(), &mut digest_len, 0) } == 0
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
}

fn windows_metadata_identity(path: &Path, metadata: &fs::Metadata) -> std::io::Result<ChangeToken> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE,
        FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    let file = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)?;
    let (token, initial_identity) = windows_open_file_change_token(&file, metadata)?;
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
    Ok(token)
}
