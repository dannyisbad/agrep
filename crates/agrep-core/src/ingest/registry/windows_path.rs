//! Windows path identity: paths compare as the filesystem resolves them, case-insensitively
//! except under directories marked case-sensitive.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::ErrorKind;
use std::os::windows::ffi::OsStrExt;
use std::path::{Component, Path, PathBuf};
use std::sync::{OnceLock, RwLock};

fn directory_case_sensitive(path: &Path) -> bool {
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
    let (value, exact) = match nearest_case_sensitivity(path) {
        Ok(found) => found,
        Err(error) => return error.kind() != ErrorKind::NotFound,
    };
    if exact {
        if let Ok(mut guard) = cache.write() {
            guard.insert(path.to_path_buf(), value);
        }
    }
    value
}

/// The case sensitivity of `path`, or of its nearest ancestor that exists (else "."), and
/// whether it was `path` itself that answered.
fn nearest_case_sensitivity(path: &Path) -> std::io::Result<(bool, bool)> {
    let mut candidate = path.to_path_buf();
    let mut exact = true;
    loop {
        if candidate.as_os_str().is_empty() {
            candidate.push(".");
            exact = false;
        }
        match probe_case_sensitive(&candidate) {
            Ok(value) => return Ok((value, exact)),
            Err(error) if error.kind() == ErrorKind::NotFound && candidate.pop() => {
                exact = false;
            }
            Err(error) => return Err(error),
        }
    }
}

fn probe_case_sensitive(candidate: &Path) -> std::io::Result<bool> {
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        FileCaseSensitiveInfo, GetFileInformationByHandleEx, FILE_CASE_SENSITIVE_INFO,
        FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    use windows_sys::Win32::System::SystemServices::FILE_CS_FLAG_CASE_SENSITIVE_DIR;

    let directory = std::fs::OpenOptions::new()
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
}

fn ordinal_eq(left: &OsStr, right: &OsStr) -> bool {
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

fn ordinal_key(value: &OsStr) -> Vec<u16> {
    let source: Vec<u16> = value.encode_wide().collect();
    let Ok(source_len) = i32::try_from(source.len()) else {
        return source;
    };
    let needed = uppercase_invariant(&source, source_len, None);
    if needed <= 0 {
        return source;
    }
    let mut mapped = vec![0_u16; needed as usize];
    let written = uppercase_invariant(&source, source_len, Some(mapped.as_mut_slice()));
    if written != needed {
        return source;
    }
    mapped
}

/// `LCMapStringEx` invariant uppercasing of `source` into `destination`, or with none the
/// length the mapping needs.
fn uppercase_invariant(source: &[u16], source_len: i32, destination: Option<&mut [u16]>) -> i32 {
    use windows_sys::Win32::Globalization::{
        LCMapStringEx, LCMAP_UPPERCASE, LOCALE_NAME_INVARIANT,
    };

    let (output, capacity) = match destination {
        Some(buffer) => (buffer.as_mut_ptr(), buffer.len() as i32),
        None => (std::ptr::null_mut(), 0),
    };
    unsafe {
        LCMapStringEx(
            LOCALE_NAME_INVARIANT,
            LCMAP_UPPERCASE,
            source.as_ptr(),
            source_len,
            output,
            capacity,
            std::ptr::null(),
            std::ptr::null(),
            0,
        )
    }
}

pub fn logical_path_key(path: &Path) -> Vec<u16> {
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

fn matching_prefix(path: &Path, root: &Path) -> Option<usize> {
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

pub fn path_eq(left: &Path, right: &Path) -> bool {
    matching_prefix(left, right).is_some()
        && left.components().count() == right.components().count()
}

pub fn relative_path(path: &Path, root: &Path) -> Option<PathBuf> {
    let path_parts: Vec<_> = path.components().collect();
    let prefix = matching_prefix(path, root)?;
    Some(path_parts[prefix..].iter().collect())
}
