use std::fs::File;
use std::io;

#[cfg(unix)]
mod platform {
    use super::{io, File};
    use std::ffi::c_void;
    use std::os::fd::AsRawFd;
    use std::ptr;

    const PROT_READ: i32 = 0x1;
    const MAP_PRIVATE: i32 = 0x2;

    unsafe extern "C" {
        fn mmap(
            address: *mut c_void,
            length: usize,
            protection: i32,
            flags: i32,
            fd: i32,
            offset: i64,
        ) -> *mut c_void;
        fn munmap(address: *mut c_void, length: usize) -> i32;
    }

    pub(crate) struct MappedFile {
        ptr: *mut u8,
        len: usize,
    }

    impl MappedFile {
        pub(crate) fn map(file: &File, len: usize) -> io::Result<Self> {
            if len == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "cannot map an empty arena",
                ));
            }
            let ptr = unsafe {
                mmap(
                    ptr::null_mut(),
                    len,
                    PROT_READ,
                    MAP_PRIVATE,
                    file.as_raw_fd(),
                    0,
                )
            };
            if ptr as isize == -1 {
                return Err(io::Error::last_os_error());
            }
            Ok(Self {
                ptr: ptr.cast(),
                len,
            })
        }

        pub(crate) fn as_slice(&self) -> &[u8] {
            unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
        }
    }

    impl Drop for MappedFile {
        fn drop(&mut self) {
            unsafe {
                munmap(self.ptr.cast(), self.len);
            }
        }
    }
}

#[cfg(windows)]
mod platform {
    use super::{io, File};
    use std::ffi::c_void;
    use std::os::windows::io::AsRawHandle;

    type Handle = *mut c_void;
    const PAGE_READONLY: u32 = 0x02;
    const FILE_MAP_READ: u32 = 0x0004;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateFileMappingW(
            file: Handle,
            attributes: *const c_void,
            protection: u32,
            maximum_size_high: u32,
            maximum_size_low: u32,
            name: *const u16,
        ) -> Handle;
        fn MapViewOfFile(
            mapping: Handle,
            access: u32,
            offset_high: u32,
            offset_low: u32,
            bytes: usize,
        ) -> *mut c_void;
        fn UnmapViewOfFile(address: *const c_void) -> i32;
        fn CloseHandle(handle: Handle) -> i32;
    }

    pub(crate) struct MappedFile {
        ptr: *mut u8,
        len: usize,
    }

    impl MappedFile {
        pub(crate) fn map(file: &File, len: usize) -> io::Result<Self> {
            if len == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "cannot map an empty arena",
                ));
            }
            let mapping = unsafe {
                CreateFileMappingW(
                    file.as_raw_handle().cast(),
                    std::ptr::null(),
                    PAGE_READONLY,
                    0,
                    0,
                    std::ptr::null(),
                )
            };
            if mapping.is_null() {
                return Err(io::Error::last_os_error());
            }
            let view = unsafe { MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, len) };
            if view.is_null() {
                // Capture the mapping error before CloseHandle clobbers thread-local last-error.
                let err = io::Error::last_os_error();
                unsafe {
                    CloseHandle(mapping);
                }
                return Err(err);
            }
            unsafe {
                CloseHandle(mapping);
            }
            Ok(Self {
                ptr: view.cast(),
                len,
            })
        }

        pub(crate) fn as_slice(&self) -> &[u8] {
            unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
        }
    }

    impl Drop for MappedFile {
        fn drop(&mut self) {
            unsafe {
                UnmapViewOfFile(self.ptr.cast());
            }
        }
    }
}

pub(crate) use platform::MappedFile;

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_PATH: AtomicU64 = AtomicU64::new(0);

    fn temp_path(label: &str) -> std::path::PathBuf {
        let suffix = NEXT_PATH.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "agrep-mapped-file-{label}-{}-{suffix}.bin",
            std::process::id()
        ))
    }

    #[test]
    fn refuses_zero_length_mapping() {
        let path = temp_path("empty");
        std::fs::write(&path, []).unwrap();
        let file = File::open(&path).unwrap();
        let error = match MappedFile::map(&file, 0) {
            Ok(_) => panic!("zero-length mapping succeeded"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        drop(file);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn maps_exact_file_bytes_and_releases_for_delete() {
        let path = temp_path("bytes");
        let payload = b"\0mapped semantic bytes\xff";
        std::fs::write(&path, payload).unwrap();
        let file = File::open(&path).unwrap();
        let mapping = MappedFile::map(&file, payload.len()).unwrap();
        assert_eq!(mapping.as_slice(), payload);
        drop(mapping);
        drop(file);
        std::fs::remove_file(path).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn unix_mapping_survives_unlink_until_drop() {
        let path = temp_path("unlink");
        let payload = b"immutable semantic generation";
        std::fs::write(&path, payload).unwrap();
        let file = File::open(&path).unwrap();
        let mapping = MappedFile::map(&file, payload.len()).unwrap();
        std::fs::remove_file(&path).unwrap();
        assert_eq!(mapping.as_slice(), payload);
        drop(mapping);
        assert_eq!(
            std::fs::metadata(path).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }
}
