//! Background work a lane's commands leave behind: finding it, ending it, and waiting for
//! it by observing process exits and directory changes, never by sleeping.
use anyhow::{bail, Result};
#[cfg(windows)]
use std::path::PathBuf;
use std::{
    path::Path,
    time::{Duration, Instant},
};
#[path = "exit.rs"]
mod exit;

/// How one lane's detached processes are recognized. On Unix every process a lane starts
/// inherits its environment, so its unique data-directory path names it. On Windows this
/// process holds all descendants in a job and the candidate executable's image tells the
/// lanes apart.
pub struct Lane {
    #[cfg(unix)]
    markers: Vec<String>,
    #[cfg(windows)]
    candidate_image: PathBuf,
    #[cfg(windows)]
    candidate: bool,
}

impl Lane {
    pub fn new(data: &Path, candidate_image: &Path, candidate: bool) -> Self {
        #[cfg(unix)]
        {
            let _ = (candidate_image, candidate);
            let mut markers = vec![data.display().to_string()];
            if let Ok(real) = data.canonicalize() {
                markers.push(real.display().to_string());
            }
            Lane { markers }
        }
        #[cfg(windows)]
        {
            let _ = data;
            Lane {
                candidate_image: candidate_image.to_path_buf(),
                candidate,
            }
        }
    }
}

/// Processes of this lane other than this one.
pub fn processes(lane: &Lane) -> Result<Vec<u32>> {
    platform::processes(lane)
}

/// Ends every process of this lane and waits until each has exited.
pub fn end(lane: &Lane, deadline: Duration) -> Result<usize> {
    let start = Instant::now();
    let mut ended = 0;
    loop {
        let pids = processes(lane)?;
        if pids.is_empty() {
            return Ok(ended);
        }
        let Some(left) = deadline.checked_sub(start.elapsed()) else {
            bail!("{} background processes outlived {deadline:?}", pids.len());
        };
        for pid in &pids {
            platform::kill(*pid);
        }
        for pid in &pids {
            wait_exit(*pid, left)?;
        }
        ended += pids.len();
    }
}

/// Waits, without ending anything, until this lane has no process left.
pub fn wait_idle(lane: &Lane, deadline: Duration) -> Result<()> {
    let start = Instant::now();
    loop {
        let pids = processes(lane)?;
        if pids.is_empty() {
            return Ok(());
        }
        let Some(left) = deadline.checked_sub(start.elapsed()) else {
            bail!(
                "{} background processes still running after {deadline:?}",
                pids.len()
            );
        };
        for pid in pids {
            wait_exit(pid, left)?;
        }
    }
}

fn wait_exit(pid: u32, left: Duration) -> Result<()> {
    anyhow::ensure!(
        exit::wait(pid, left)?,
        "process {pid} did not exit within {left:?}"
    );
    Ok(())
}

/// Waits until `path` no longer exists, waking on every change to its directory. Returns
/// false at the deadline, or as soon as no process of the lane is left to remove it.
pub fn wait_absent(path: &Path, lane: &Lane, deadline: Duration) -> Result<bool> {
    let watch = platform::DirWatch::new(path.parent().unwrap_or(Path::new(".")))?;
    let start = Instant::now();
    loop {
        if std::fs::symlink_metadata(path).is_err() {
            return Ok(true);
        }
        let Some(left) = deadline.checked_sub(start.elapsed()) else {
            return Ok(false);
        };
        // A quiet directory is checked for an owner every ten seconds.
        if !watch.wait(left.min(Duration::from_secs(10)))? && processes(lane)?.is_empty() {
            return Ok(std::fs::symlink_metadata(path).is_err());
        }
    }
}

/// Holds every descendant in a job on Windows so detached work can always be found.
pub fn init() -> Result<()> {
    platform::init()
}

#[cfg(unix)]
mod platform {
    use super::Lane;
    use anyhow::Result;
    use std::time::Duration;

    pub fn init() -> Result<()> {
        Ok(())
    }

    #[cfg(target_os = "macos")]
    pub fn processes(lane: &Lane) -> Result<Vec<u32>> {
        // KERN_PROCARGS2 through ps shows the exec-time environment of this user's processes.
        let out = std::process::Command::new("/bin/ps")
            .args(["-E", "-ww", "-A", "-o", "pid=,command="])
            .output()?;
        anyhow::ensure!(out.status.success(), "ps failed: {}", out.status);
        let own = std::process::id();
        Ok(String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter_map(|line| {
                let line = line.trim_start();
                let (pid, rest) = line.split_once(' ')?;
                let pid: u32 = pid.parse().ok()?;
                (pid != own && lane.markers.iter().any(|m| rest.contains(m.as_str())))
                    .then_some(pid)
            })
            .collect())
    }

    #[cfg(not(target_os = "macos"))]
    pub fn processes(lane: &Lane) -> Result<Vec<u32>> {
        let own = std::process::id();
        let mut found = Vec::new();
        for entry in std::fs::read_dir("/proc")? {
            let Ok(entry) = entry else { continue };
            let Some(pid) = entry
                .file_name()
                .to_str()
                .and_then(|s| s.parse::<u32>().ok())
            else {
                continue;
            };
            if pid == own {
                continue;
            }
            let mut text = Vec::new();
            for part in ["cmdline", "environ"] {
                if let Ok(bytes) = std::fs::read(entry.path().join(part)) {
                    text.extend(bytes);
                }
            }
            let text = String::from_utf8_lossy(&text);
            if lane.markers.iter().any(|m| text.contains(m.as_str())) {
                found.push(pid);
            }
        }
        Ok(found)
    }

    pub fn kill(pid: u32) {
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGKILL);
        }
    }

    #[cfg(target_os = "macos")]
    fn timespec(left: Duration) -> libc::timespec {
        libc::timespec {
            tv_sec: left.as_secs() as libc::time_t,
            tv_nsec: left.subsec_nanos() as libc::c_long,
        }
    }

    struct Fd(libc::c_int);
    impl Drop for Fd {
        fn drop(&mut self) {
            if self.0 >= 0 {
                unsafe {
                    libc::close(self.0);
                }
            }
        }
    }

    pub struct DirWatch {
        kq: Fd,
        _dir: Fd,
    }

    #[cfg(target_os = "macos")]
    impl DirWatch {
        pub fn new(dir: &std::path::Path) -> Result<Self> {
            use std::os::unix::ffi::OsStrExt;
            let path = std::ffi::CString::new(dir.as_os_str().as_bytes())?;
            let dir = Fd(unsafe { libc::open(path.as_ptr(), libc::O_EVTONLY) });
            anyhow::ensure!(dir.0 >= 0, "open: {}", std::io::Error::last_os_error());
            let kq = Fd(unsafe { libc::kqueue() });
            anyhow::ensure!(kq.0 >= 0, "kqueue: {}", std::io::Error::last_os_error());
            let change = libc::kevent {
                ident: dir.0 as libc::uintptr_t,
                filter: libc::EVFILT_VNODE,
                flags: libc::EV_ADD | libc::EV_CLEAR,
                fflags: libc::NOTE_WRITE,
                data: 0,
                udata: std::ptr::null_mut(),
            };
            let n = unsafe {
                libc::kevent(kq.0, &change, 1, std::ptr::null_mut(), 0, std::ptr::null())
            };
            anyhow::ensure!(n >= 0, "kevent: {}", std::io::Error::last_os_error());
            Ok(Self { kq, _dir: dir })
        }

        /// True when the directory changed, false when `left` passed quietly.
        pub fn wait(&self, left: Duration) -> Result<bool> {
            let mut event: libc::kevent = unsafe { std::mem::zeroed() };
            let timeout = timespec(left);
            let n =
                unsafe { libc::kevent(self.kq.0, std::ptr::null(), 0, &mut event, 1, &timeout) };
            anyhow::ensure!(n >= 0, "kevent: {}", std::io::Error::last_os_error());
            Ok(n > 0)
        }
    }

    #[cfg(not(target_os = "macos"))]
    impl DirWatch {
        pub fn new(dir: &std::path::Path) -> Result<Self> {
            use std::os::unix::ffi::OsStrExt;
            let path = std::ffi::CString::new(dir.as_os_str().as_bytes())?;
            let kq = Fd(unsafe { libc::inotify_init1(libc::IN_CLOEXEC | libc::IN_NONBLOCK) });
            anyhow::ensure!(kq.0 >= 0, "inotify: {}", std::io::Error::last_os_error());
            let mask = libc::IN_DELETE | libc::IN_MOVED_FROM | libc::IN_MOVED_TO | libc::IN_CREATE;
            let n = unsafe { libc::inotify_add_watch(kq.0, path.as_ptr(), mask) };
            anyhow::ensure!(
                n >= 0,
                "inotify_add_watch: {}",
                std::io::Error::last_os_error()
            );
            Ok(Self { kq, _dir: Fd(-1) })
        }

        /// True when the directory changed, false when `left` passed quietly.
        pub fn wait(&self, left: Duration) -> Result<bool> {
            let mut poll = libc::pollfd {
                fd: self.kq.0,
                events: libc::POLLIN,
                revents: 0,
            };
            let millis = left.as_millis().min(i32::MAX as u128) as libc::c_int;
            let n = unsafe { libc::poll(&mut poll, 1, millis) };
            anyhow::ensure!(n >= 0, "poll: {}", std::io::Error::last_os_error());
            let mut buffer = [0u8; 4096];
            while unsafe { libc::read(self.kq.0, buffer.as_mut_ptr().cast(), buffer.len()) } > 0 {}
            Ok(n > 0)
        }
    }
}

#[cfg(windows)]
mod platform {
    use super::Lane;
    use anyhow::Result;
    use std::{os::windows::ffi::OsStrExt, path::Path, sync::OnceLock, time::Duration};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0, WAIT_TIMEOUT},
        Storage::FileSystem::{
            FindCloseChangeNotification, FindFirstChangeNotificationW, FindNextChangeNotification,
            FILE_NOTIFY_CHANGE_FILE_NAME,
        },
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicProcessIdList,
                JobObjectExtendedLimitInformation, QueryInformationJobObject,
                SetInformationJobObject, JOBOBJECT_BASIC_PROCESS_ID_LIST,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::{
                GetCurrentProcess, GetCurrentProcessId, OpenProcess, QueryFullProcessImageNameW,
                TerminateProcess, WaitForSingleObject, PROCESS_NAME_WIN32,
                PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
            },
        },
    };

    struct Job(HANDLE);
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}
    static JOB: OnceLock<Job> = OnceLock::new();

    pub fn init() -> Result<()> {
        if JOB.get().is_some() {
            return Ok(());
        }
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            anyhow::ensure!(!job.is_null(), "job: {}", std::io::Error::last_os_error());
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            // No breakaway: work a reference command detaches stays in this job.
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of_val(&info) as u32,
            ) == 0
                || AssignProcessToJobObject(job, GetCurrentProcess()) == 0
            {
                let error = std::io::Error::last_os_error();
                CloseHandle(job);
                anyhow::bail!("job: {error}");
            }
            let _ = JOB.set(Job(job));
        }
        Ok(())
    }

    struct Process(HANDLE);
    impl Drop for Process {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
    fn open(pid: u32) -> Option<Process> {
        let handle = unsafe {
            OpenProcess(
                PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
                0,
                pid,
            )
        };
        (!handle.is_null()).then_some(Process(handle))
    }
    fn image(process: &Process) -> Option<std::path::PathBuf> {
        let mut buffer = vec![0u16; 32768];
        let mut size = buffer.len() as u32;
        let ok = unsafe {
            QueryFullProcessImageNameW(
                process.0,
                PROCESS_NAME_WIN32,
                buffer.as_mut_ptr(),
                &mut size,
            )
        };
        (ok != 0).then(|| String::from_utf16_lossy(&buffer[..size as usize]).into())
    }
    fn same_file(a: &Path, b: &Path) -> bool {
        match (a.canonicalize(), b.canonicalize()) {
            (Ok(a), Ok(b)) => a == b,
            _ => a.as_os_str().eq_ignore_ascii_case(b.as_os_str()),
        }
    }

    pub fn processes(lane: &Lane) -> Result<Vec<u32>> {
        let Some(job) = JOB.get() else {
            anyhow::bail!("process job not initialized");
        };
        const MAX: usize = 4096;
        let mut buffer = vec![0usize; MAX + 2];
        let ok = unsafe {
            QueryInformationJobObject(
                job.0,
                JobObjectBasicProcessIdList,
                buffer.as_mut_ptr().cast(),
                std::mem::size_of_val(buffer.as_slice()) as u32,
                std::ptr::null_mut(),
            )
        };
        anyhow::ensure!(
            ok != 0,
            "job processes: {}",
            std::io::Error::last_os_error()
        );
        let base = buffer.as_ptr().cast::<u8>();
        let (listed, ids) = unsafe {
            let header = base.cast::<JOBOBJECT_BASIC_PROCESS_ID_LIST>().read();
            let ids = base
                .add(std::mem::offset_of!(
                    JOBOBJECT_BASIC_PROCESS_ID_LIST,
                    ProcessIdList
                ))
                .cast::<usize>();
            (header.NumberOfProcessIdsInList as usize, ids)
        };
        let ids = unsafe { std::slice::from_raw_parts(ids, listed.min(MAX)) };
        let own = unsafe { GetCurrentProcessId() };
        let mut found = Vec::new();
        for &id in ids {
            let pid = id as u32;
            if pid == own {
                continue;
            }
            let Some(process) = open(pid) else { continue };
            let is_candidate =
                image(&process).is_some_and(|path| same_file(&path, &lane.candidate_image));
            if is_candidate == lane.candidate {
                found.push(pid);
            }
        }
        Ok(found)
    }

    pub fn kill(pid: u32) {
        if let Some(process) = open(pid) {
            unsafe {
                TerminateProcess(process.0, 1);
            }
        }
    }

    pub struct DirWatch(HANDLE);
    impl Drop for DirWatch {
        fn drop(&mut self) {
            unsafe {
                FindCloseChangeNotification(self.0);
            }
        }
    }
    impl DirWatch {
        pub fn new(dir: &Path) -> Result<Self> {
            let wide: Vec<u16> = dir.as_os_str().encode_wide().chain([0]).collect();
            let handle = unsafe {
                FindFirstChangeNotificationW(wide.as_ptr(), 0, FILE_NOTIFY_CHANGE_FILE_NAME)
            };
            anyhow::ensure!(
                !handle.is_null() && handle != INVALID_HANDLE_VALUE,
                "change notification: {}",
                std::io::Error::last_os_error()
            );
            Ok(Self(handle))
        }

        /// True when the directory changed, false when `left` passed quietly.
        pub fn wait(&self, left: Duration) -> Result<bool> {
            let millis = left.as_millis().min(u32::MAX as u128 - 1) as u32;
            match unsafe { WaitForSingleObject(self.0, millis) } {
                WAIT_OBJECT_0 => {
                    anyhow::ensure!(
                        unsafe { FindNextChangeNotification(self.0) } != 0,
                        "change notification: {}",
                        std::io::Error::last_os_error()
                    );
                    Ok(true)
                }
                WAIT_TIMEOUT => Ok(false),
                _ => anyhow::bail!("wait: {}", std::io::Error::last_os_error()),
            }
        }
    }
}
