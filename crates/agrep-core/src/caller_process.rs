//! Kernel process ancestry and birth identities used to reject stale publications.
#[derive(Debug)]
pub struct Process {
    pub parent: u32,
    pub identity: String,
    pub born_ms: i64,
}
#[cfg(target_os = "linux")]
pub fn info(pid: u32) -> Option<Process> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let fields: Vec<_> = stat.rsplit_once(')')?.1.split_whitespace().collect();
    let ticks: u64 = fields.get(19)?.parse().ok()?;
    let system = std::fs::read_to_string("/proc/stat").ok()?;
    let boot: i64 = system
        .lines()
        .find_map(|l| l.strip_prefix("btime "))?
        .parse()
        .ok()?;
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    if hz <= 0 {
        return None;
    }
    Some(Process {
        parent: fields.get(1)?.parse().ok()?,
        identity: format!("proc_{ticks}"),
        born_ms: boot * 1000 + (ticks * 1000 / hz as u64) as i64,
    })
}
#[cfg(target_os = "macos")]
pub fn info(pid: u32) -> Option<Process> {
    #[repr(C)]
    #[derive(Default)]
    struct BsdInfo {
        flags: u32,
        status: u32,
        xstatus: u32,
        pid: u32,
        ppid: u32,
        uid: u32,
        gid: u32,
        ruid: u32,
        rgid: u32,
        svuid: u32,
        svgid: u32,
        reserved: u32,
        comm: [u8; 16],
        name: [u8; 32],
        nfiles: u32,
        pgid: u32,
        pjobc: u32,
        tdev: u32,
        tpgid: u32,
        nice: i32,
        start_sec: u64,
        start_usec: u64,
    }
    #[link(name = "proc")]
    extern "C" {
        fn proc_pidinfo(
            pid: i32,
            flavor: i32,
            arg: u64,
            buffer: *mut std::ffi::c_void,
            size: i32,
        ) -> i32;
    }
    let mut v = BsdInfo::default();
    let size = std::mem::size_of::<BsdInfo>() as i32;
    if unsafe { proc_pidinfo(pid as i32, 3, 0, (&mut v as *mut BsdInfo).cast(), size) } != size
        || v.start_sec == 0
    {
        return None;
    }
    Some(Process {
        parent: v.ppid,
        identity: format!("darwin_{}_{}", v.start_sec, v.start_usec),
        born_ms: (v.start_sec * 1000 + v.start_usec / 1000) as i64,
    })
}
#[cfg(target_os = "windows")]
pub fn info(pid: u32) -> Option<Process> {
    use std::ffi::c_void;
    #[repr(C)]
    #[derive(Default)]
    struct FileTime {
        low: u32,
        high: u32,
    }
    #[repr(C)]
    #[derive(Default)]
    struct Basic {
        reserved: *mut c_void,
        peb: *mut c_void,
        reserved2: [*mut c_void; 2],
        pid: usize,
        parent: usize,
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn CloseHandle(handle: *mut c_void) -> i32;
        fn GetProcessTimes(
            handle: *mut c_void,
            creation: *mut FileTime,
            exit: *mut FileTime,
            kernel: *mut FileTime,
            user: *mut FileTime,
        ) -> i32;
    }
    #[link(name = "ntdll")]
    extern "system" {
        fn NtQueryInformationProcess(
            handle: *mut c_void,
            class: u32,
            info: *mut c_void,
            len: u32,
            written: *mut u32,
        ) -> i32;
    }
    unsafe {
        let handle = OpenProcess(0x1000, 0, pid);
        if handle.is_null() {
            return None;
        }
        let (mut c, mut e, mut k, mut u) = (
            FileTime::default(),
            FileTime::default(),
            FileTime::default(),
            FileTime::default(),
        );
        let mut basic = Basic::default();
        let mut written = 0;
        let valid = GetProcessTimes(handle, &mut c, &mut e, &mut k, &mut u) != 0
            && NtQueryInformationProcess(
                handle,
                0,
                (&mut basic as *mut Basic).cast(),
                std::mem::size_of::<Basic>() as u32,
                &mut written,
            ) == 0;
        CloseHandle(handle);
        if !valid {
            return None;
        }
        let ticks = (u64::from(c.high) << 32) | u64::from(c.low);
        Some(Process {
            parent: basic.parent.try_into().ok()?,
            identity: format!("win_{ticks}"),
            born_ms: (ticks / 10000) as i64 - 11644473600000,
        })
    }
}
#[cfg(not(any(target_os = "windows", target_os = "linux", target_os = "macos")))]
pub fn info(_pid: u32) -> Option<Process> {
    None
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn current_process_has_kernel_birth() {
        let p = info(std::process::id()).expect("supported platform supplies process metadata");
        assert!(p.parent > 0);
        assert!(p.born_ms > 0);
        assert!(!p.identity.is_empty());
    }
}
