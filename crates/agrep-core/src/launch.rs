//! Native agent commands and owned foreground processes.
use crate::history::{self, Session};
use anyhow::{bail, Context, Result};
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    process::Command,
};
/// Agents without native resume and why, from 0.4.9's registry
/// (py/hookless/agent_registry.json).
const NO_RESUME: &[(&str, &str)] = &[
    ("kimi", "no verified resume-by-session CLI contract"),
    (
        "cline",
        "tasks are editor-owned and have no standalone resume command",
    ),
    ("gemini", "no verified resume-by-session CLI contract"),
    ("crush", "no verified resume-by-session CLI contract"),
    (
        "cursor",
        "sessions are editor-owned and have no safe CLI resume command",
    ),
];
const RESUMABLE: [&str; 5] = ["claude", "codex", "opencode", "antigravity", "pi"];
pub fn capability(agent: &str) -> Result<(&'static str, &'static str)> {
    resolve(agent, "native resume")
}
/// `agrep run`'s check: it launches the agents native resume serves; any other name is
/// refused for the command it was given to.
pub fn launchable(agent: &str) -> Result<()> {
    let name = history::normalize_agent(agent);
    if NO_RESUME.iter().any(|(known, _)| *known == name) {
        bail!(
            "{name} can't be launched by agrep run; supported for agrep run: {}",
            RESUMABLE.join(", ")
        );
    }
    resolve(agent, "agrep run").map(drop)
}
fn resolve(agent: &str, purpose: &str) -> Result<(&'static str, &'static str)> {
    let name = history::normalize_agent(agent);
    match name {
        "claude" => Ok(("claude", "--resume")),
        "codex" => Ok(("codex", "resume")),
        "opencode" => Ok(("opencode", "--session")),
        "antigravity" => Ok(("agy", "--conversation")),
        "pi" => Ok(("pi", "-r")),
        _ => bail!(
            "{}",
            history::capability_error(purpose, name, &RESUMABLE, NO_RESUME)
        ),
    }
}
pub fn executable(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        #[cfg(not(windows))]
        let names = vec![OsString::from(name)];
        #[cfg(windows)]
        let names = {
            let exts = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
            exts.split(';')
                .filter(|ext| !ext.is_empty())
                .map(|ext| OsString::from(format!("{name}{ext}")))
                .collect::<Vec<_>>()
        };
        for name in names {
            let p = dir.join(name);
            if p.is_file() {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    if std::fs::metadata(&p).ok()?.permissions().mode() & 0o111 == 0 {
                        continue;
                    }
                }
                return Some(p);
            }
        }
    }
    None
}
pub fn choose_executable(agent: &str, preferred: Option<&str>) -> Result<Option<PathBuf>> {
    let (name, _) = capability(agent)?;
    if history::normalize_agent(agent) == "pi" {
        let name = preferred.unwrap_or(if agent == "omp" { "omp" } else { "pi" });
        Ok(executable(name).or_else(|| executable(if name == "omp" { "pi" } else { "omp" })))
    } else {
        Ok(executable(name))
    }
}
/// The agent a launch of `exe` starts: for the pi family, the fork `exe` runs.
pub fn launched_agent<'a>(agent: &'a str, exe: &'a Path) -> std::borrow::Cow<'a, str> {
    match exe.file_stem() {
        Some(fork) if history::normalize_agent(agent) == "pi" => fork.to_string_lossy(),
        _ => agent.into(),
    }
}
pub fn resume_args(s: &Session) -> Result<Vec<OsString>> {
    if !crate::handles::plain(&s.id) {
        bail!("invalid session id for native resume");
    }
    if !Path::new(&s.cwd).is_absolute() {
        bail!("session working directory must be absolute");
    }
    let (_, flag) = capability(&s.agent)?;
    let mut args = vec![];
    if s.agent == "opencode" {
        args.push(s.cwd.clone().into());
    }
    args.push(flag.into());
    args.push(s.id.clone().into());
    Ok(args)
}
pub fn run(exe: &Path, args: &[OsString], cwd: &Path) -> Result<i32> {
    if !cwd.is_dir() {
        bail!("working directory does not exist: {}", cwd.display())
    }
    let mut command = Command::new(exe);
    command.args(args).current_dir(cwd);
    // The agent owns a group in this session: run can end its descendants, and the
    // terminal's job control reaches it.
    #[cfg(unix)]
    std::os::unix::process::CommandExt::process_group(&mut command, 0);
    #[cfg(unix)]
    let interactive = unsafe { libc::isatty(0) == 1 && libc::isatty(1) == 1 };
    #[cfg(windows)]
    windows_job()?;
    #[cfg(windows)]
    let signals = windows::Signals::install()?;
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x00000200);
    }
    #[cfg(unix)]
    let mut signals = unix::Signals::install(interactive)?;
    let mut child = command
        .spawn()
        .with_context(|| format!("could not launch {}", exe.display()))?;
    #[cfg(unix)]
    signals.started(child.id())?;
    #[cfg(windows)]
    signals.child(child.id());
    #[cfg(unix)]
    signals.wait_for_exit(child.id())?;
    #[cfg(unix)]
    signals.finish(child.id());
    let status = child.wait()?;
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt;
        let signal = signals.signal();
        if signal != 0 {
            128 + signal
        } else {
            status
                .code()
                .unwrap_or_else(|| 128 + status.signal().unwrap_or(1))
        }
    };
    #[cfg(windows)]
    let code = if signals.signal() != 0 {
        128 + signals.signal()
    } else {
        match status.code() {
            Some(-1073741510) => 130,
            Some(code) => code,
            None => 1,
        }
    };
    #[cfg(not(any(unix, windows)))]
    let code = status.code().unwrap_or(1);
    Ok(code)
}
#[cfg(unix)]
mod unix {
    use anyhow::Result;
    use std::{
        sync::atomic::{AtomicBool, AtomicI32, Ordering},
        time::{Duration, Instant},
    };
    static PID: AtomicI32 = AtomicI32::new(0);
    static SIGNAL: AtomicI32 = AtomicI32::new(0);
    static SUSPEND: AtomicBool = AtomicBool::new(false);
    extern "C" fn relay(sig: i32) {
        let _ = SIGNAL.compare_exchange(0, sig, Ordering::SeqCst, Ordering::SeqCst);
        let pid = PID.load(Ordering::SeqCst);
        if pid > 0 {
            unsafe {
                libc::kill(-pid, sig);
            }
        }
    }
    /// Interrupts the wait when this process is continued, perhaps as the terminal's new
    /// foreground. macOS does not wake a stopped parent's waitid for a child that stops
    /// meanwhile, so this is also the wakeup that notices such a stop.
    extern "C" fn wake(_: i32) {}
    extern "C" fn suspend(_: i32) {
        SUSPEND.store(true, Ordering::SeqCst);
        let pid = PID.load(Ordering::SeqCst);
        if pid > 0 {
            unsafe {
                libc::kill(-pid, libc::SIGTSTP);
            }
        }
    }
    fn continue_group(pid: u32) {
        unsafe {
            libc::kill(-(pid as i32), libc::SIGCONT);
            // A pending suspend needs a fresh stop notification: this continue may have
            // canceled its signal, and the caller may have consumed the previous stop.
            if SUSPEND.load(Ordering::SeqCst) {
                libc::kill(-(pid as i32), libc::SIGTSTP);
            }
        }
    }
    fn holds_terminal() -> bool {
        unsafe { libc::tcgetpgrp(0) == libc::getpgrp() }
    }
    pub struct Signals {
        old: Vec<(i32, libc::sigaction)>,
        /// Whether the agent's group is lent the terminal while this process holds it. An
        /// interactive agent is lent it at once. In a pipeline (`agrep run x | less`) the
        /// terminal stays with the whole job, so a partner reading /dev/tty is not stopped,
        /// until the agent itself reads or configures the terminal.
        wants_terminal: bool,
        termios: Option<libc::termios>,
    }
    impl Signals {
        pub fn install(interactive: bool) -> Result<Self> {
            let mut s = Self {
                old: vec![],
                wants_terminal: interactive,
                termios: {
                    let mut t = unsafe { std::mem::zeroed() };
                    if unsafe { libc::tcgetattr(0, &mut t) } == 0 {
                        Some(t)
                    } else {
                        None
                    }
                },
            };
            SIGNAL.store(0, Ordering::SeqCst);
            SUSPEND.store(false, Ordering::SeqCst);
            for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP, libc::SIGQUIT] {
                s.handle(sig, relay as *const () as usize)?;
            }
            s.handle(libc::SIGCONT, wake as *const () as usize)?;
            s.handle(libc::SIGTSTP, suspend as *const () as usize)?;
            Ok(s)
        }
        fn handle(&mut self, sig: i32, action: libc::sighandler_t) -> Result<()> {
            let mut new: libc::sigaction = unsafe { std::mem::zeroed() };
            new.sa_sigaction = action;
            unsafe { libc::sigemptyset(&mut new.sa_mask) };
            let mut old = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigaction(sig, &new, &mut old) } != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            self.old.push((sig, old));
            Ok(())
        }
        /// Ignoring SIGTTOU lets this process take the terminal back from the agent's group.
        /// It starts only after the spawn, because an ignored signal survives exec and the
        /// agent must stop, as under 0.4.9, when it touches the terminal from the background.
        pub fn started(&mut self, pid: u32) -> Result<()> {
            self.foreground(pid);
            self.handle(libc::SIGTTOU, libc::SIG_IGN)
        }
        /// True when it lent the terminal, which also continues the agent.
        fn foreground(&mut self, pid: u32) -> bool {
            PID.store(pid as i32, Ordering::SeqCst);
            if SUSPEND.load(Ordering::SeqCst) {
                unsafe {
                    libc::kill(-(pid as i32), libc::SIGTSTP);
                }
            }
            let lent = self.lend(pid);
            let sig = self.signal();
            if sig != 0 {
                unsafe {
                    libc::kill(-(pid as i32), sig);
                }
            }
            lent
        }
        /// Lends the terminal to the agent's group while this process holds it, and continues
        /// the group: a member may have stopped touching the terminal before the handoff. `fg`
        /// hands a running background job the terminal without signalling it, so every wakeup
        /// asks again, as 0.4.9's poll did. True when it lent the terminal.
        fn lend(&self, pid: u32) -> bool {
            let group = pid as i32;
            if !self.wants_terminal
                || SUSPEND.load(Ordering::SeqCst)
                || !holds_terminal()
                || unsafe { libc::tcsetpgrp(0, group) } != 0
            {
                return false;
            }
            continue_group(pid);
            true
        }
        fn holds(&self, pid: u32) -> bool {
            self.wants_terminal && unsafe { libc::tcgetpgrp(0) } == pid as i32
        }
        /// Takes the terminal back from the agent's group only: with SIGTTOU ignored, a
        /// background agrep could otherwise take it from the shell.
        fn take_back(&self, pid: u32) {
            if self.holds(pid) {
                unsafe {
                    libc::tcsetpgrp(0, libc::getpgrp());
                }
            }
        }
        pub fn signal(&self) -> i32 {
            SIGNAL.load(Ordering::SeqCst)
        }
        /// Leave an exited leader unreaped until descendants are drained, preventing PID reuse.
        pub fn wait_for_exit(&mut self, pid: u32) -> Result<()> {
            loop {
                let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
                if unsafe {
                    libc::waitid(
                        libc::P_PID,
                        pid,
                        &mut info,
                        libc::WEXITED | libc::WSTOPPED | libc::WNOWAIT,
                    )
                } != 0
                {
                    let error = std::io::Error::last_os_error();
                    if error.kind() == std::io::ErrorKind::Interrupted {
                        self.lend(pid);
                        continue;
                    }
                    return Err(error.into());
                }
                if info.si_code == libc::CLD_STOPPED {
                    let mut status = 0;
                    unsafe {
                        libc::waitpid(pid as i32, &mut status, libc::WUNTRACED);
                    }
                    // Reading or configuring the terminal outside its foreground stopped the
                    // agent; once the terminal is this job's, the agent just goes on.
                    if !SUSPEND.load(Ordering::SeqCst)
                        && libc::WIFSTOPPED(status)
                        && matches!(libc::WSTOPSIG(status), libc::SIGTTIN | libc::SIGTTOU)
                    {
                        self.wants_terminal = true;
                        if self.lend(pid) {
                            continue;
                        }
                        if self.holds(pid) {
                            continue_group(pid);
                            continue;
                        }
                    }
                    self.take_back(pid);
                    unsafe {
                        let mut default: libc::sigaction = std::mem::zeroed();
                        default.sa_sigaction = libc::SIG_DFL;
                        libc::sigemptyset(&mut default.sa_mask);
                        let mut previous = std::mem::zeroed();
                        if libc::sigaction(libc::SIGTSTP, &default, &mut previous) != 0 {
                            return Err(std::io::Error::last_os_error().into());
                        }
                        SUSPEND.store(false, Ordering::SeqCst);
                        libc::kill(0, libc::SIGTSTP);
                        libc::sigaction(libc::SIGTSTP, &previous, std::ptr::null_mut());
                    }
                    // One SIGCONT only: on macOS, a second one right after the lend's now and
                    // then left the agent ignoring its next Ctrl-Z.
                    if !self.foreground(pid) {
                        continue_group(pid);
                    }
                } else {
                    return Ok(());
                }
            }
        }
        pub fn finish(&mut self, pid: u32) {
            self.take_back(pid);
            // wait_for_exit uses WNOWAIT: this child still reserves the PID,
            // even on systems where getpgid no longer resolves an exited leader.
            unsafe {
                libc::kill(-(pid as i32), libc::SIGTERM);
            }
            drain(pid as i32, Duration::from_millis(1500));
            unsafe {
                libc::kill(-(pid as i32), libc::SIGKILL);
            }
            PID.store(0, Ordering::SeqCst);
        }
    }
    impl Drop for Signals {
        fn drop(&mut self) {
            let pid = PID.swap(0, Ordering::SeqCst);
            if pid > 0 {
                unsafe {
                    libc::kill(-pid, libc::SIGKILL);
                }
                self.take_back(pid as u32);
            }
            // In the background the modes are the shell's, which it may have changed since.
            if let Some(t) = self.termios.as_ref().filter(|_| holds_terminal()) {
                unsafe {
                    libc::tcsetattr(0, libc::TCSANOW, t);
                }
            }
            for (sig, old) in &self.old {
                unsafe {
                    libc::sigaction(*sig, old, std::ptr::null_mut());
                }
            }
            PID.store(0, Ordering::SeqCst);
        }
    }

    /// Gives every member of group `leader` other than the exited, unreaped leader up to
    /// `grace` to exit, returning as soon as none is left.
    fn drain(leader: i32, grace: Duration) {
        let deadline = Instant::now() + grace;
        while member_exited(leader, deadline.saturating_duration_since(Instant::now())) {}
    }

    /// Waits up to `left` for a live member of group `leader` to exit. True means the group
    /// is worth checking again; false means no member is left or none exited in time.
    #[cfg(target_os = "macos")]
    fn member_exited(leader: i32, left: Duration) -> bool {
        use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
        let mut pids = vec![0 as libc::pid_t; 64];
        let listed = loop {
            let size = std::mem::size_of_val(pids.as_slice()) as libc::c_int;
            let n = unsafe { libc::proc_listpgrppids(leader, pids.as_mut_ptr().cast(), size) };
            if n < 0 {
                return false;
            }
            if (n as usize) < pids.len() {
                break n as usize;
            }
            pids.resize(pids.len() * 2, 0);
        };
        let changes: Vec<_> = pids[..listed]
            .iter()
            .filter(|&&pid| pid > 0 && pid != leader)
            .map(|&pid| libc::kevent {
                ident: pid as libc::uintptr_t,
                filter: libc::EVFILT_PROC,
                flags: libc::EV_ADD | libc::EV_ONESHOT | libc::EV_RECEIPT,
                fflags: libc::NOTE_EXIT,
                data: 0,
                udata: std::ptr::null_mut(),
            })
            .collect();
        if changes.is_empty() {
            return false;
        }
        let kq = unsafe { libc::kqueue() };
        if kq < 0 {
            return false;
        }
        let kq = unsafe { OwnedFd::from_raw_fd(kq) };
        let mut receipts = changes.clone();
        let now = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        let n = unsafe {
            libc::kevent(
                kq.as_raw_fd(),
                changes.as_ptr(),
                changes.len() as libc::c_int,
                receipts.as_mut_ptr(),
                receipts.len() as libc::c_int,
                &now,
            )
        };
        // A receipt's data is 0 for a watched member and ESRCH for one that already exited,
        // which includes the zombies proc_listpgrppids lists.
        if n <= 0 || !receipts[..n as usize].iter().any(|r| r.data == 0) {
            return false;
        }
        let timeout = libc::timespec {
            tv_sec: left.as_secs() as libc::time_t,
            tv_nsec: left.subsec_nanos() as libc::c_long,
        };
        let mut event = changes[0];
        woke(unsafe { libc::kevent(kq.as_raw_fd(), std::ptr::null(), 0, &mut event, 1, &timeout) })
    }

    /// Waits up to `left` for a live member of group `leader` to exit. True means the group
    /// is worth checking again; false means no member is left or none exited in time.
    #[cfg(target_os = "linux")]
    fn member_exited(leader: i32, left: Duration) -> bool {
        use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
        let Ok(entries) = std::fs::read_dir("/proc") else {
            return false;
        };
        let mut members = vec![];
        for entry in entries.flatten() {
            let Some(pid) = entry
                .file_name()
                .to_str()
                .and_then(|s| s.parse::<libc::pid_t>().ok())
            else {
                continue;
            };
            let Ok(stat) = std::fs::read_to_string(entry.path().join("stat")) else {
                continue;
            };
            // State, parent and process group follow the parenthesized command name.
            let mut fields = stat
                .rsplit_once(')')
                .map_or("", |(_, rest)| rest)
                .split_whitespace();
            let (state, group) = (fields.next(), fields.nth(1));
            if pid == leader
                || state == Some("Z")
                || group.and_then(|g| g.parse::<libc::pid_t>().ok()) != Some(leader)
            {
                continue;
            }
            let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
            if fd >= 0 {
                members.push(unsafe { OwnedFd::from_raw_fd(fd as libc::c_int) });
            } else if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOSYS) {
                // Before Linux 5.3 no exit can be awaited, so the group gets its whole grace.
                std::thread::sleep(left);
                return false;
            }
        }
        if members.is_empty() {
            return false;
        }
        let mut polls: Vec<_> = members
            .iter()
            .map(|fd| libc::pollfd {
                fd: fd.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            })
            .collect();
        let millis = left.as_millis().min(i32::MAX as u128) as libc::c_int;
        woke(unsafe { libc::poll(polls.as_mut_ptr(), polls.len() as libc::nfds_t, millis) })
    }

    /// Whether a kevent or poll result reports an event, or a signal that interrupted it.
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    fn woke(result: libc::c_int) -> bool {
        let interrupted =
            || std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted;
        result > 0 || (result < 0 && interrupted())
    }

    /// Without an exit notification for other processes, the group gets its whole grace.
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    fn member_exited(_: i32, left: Duration) -> bool {
        std::thread::sleep(left);
        false
    }
}
#[cfg(windows)]
fn windows_job() -> Result<()> {
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::GetCurrentProcess,
        },
    };
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(std::io::Error::last_os_error().into());
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const _,
            std::mem::size_of_val(&info) as u32,
        ) == 0
            || AssignProcessToJobObject(job, GetCurrentProcess()) == 0
        {
            CloseHandle(job);
            return Err(std::io::Error::last_os_error().into());
        }
        // Keep the sole non-inheritable job handle until process exit. The kernel then kills all remaining children.
    }
    Ok(())
}

#[cfg(windows)]
mod windows {
    use anyhow::Result;
    use std::sync::atomic::{AtomicI32, AtomicU32, Ordering};
    use windows_sys::Win32::System::Console::{
        GenerateConsoleCtrlEvent, SetConsoleCtrlHandler, CTRL_BREAK_EVENT,
    };
    static PID: AtomicU32 = AtomicU32::new(0);
    static SIGNAL: AtomicI32 = AtomicI32::new(0);
    unsafe extern "system" fn relay(event: u32) -> i32 {
        let signal = match event {
            0 => 2,
            1 => 3,
            2 | 5 | 6 => 15,
            _ => return 0,
        };
        let _ = SIGNAL.compare_exchange(0, signal, Ordering::SeqCst, Ordering::SeqCst);
        let pid = PID.load(Ordering::SeqCst);
        if pid != 0 {
            unsafe {
                GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid);
            }
        }
        1
    }
    pub struct Signals;
    impl Signals {
        pub fn install() -> Result<Self> {
            SIGNAL.store(0, Ordering::SeqCst);
            if unsafe { SetConsoleCtrlHandler(Some(relay), 1) } == 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(Self)
        }
        pub fn child(&self, pid: u32) {
            PID.store(pid, Ordering::SeqCst);
            if self.signal() != 0 {
                unsafe {
                    GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid);
                }
            }
        }
        pub fn signal(&self) -> i32 {
            SIGNAL.load(Ordering::SeqCst)
        }
    }
    impl Drop for Signals {
        fn drop(&mut self) {
            PID.store(0, Ordering::SeqCst);
            unsafe {
                SetConsoleCtrlHandler(Some(relay), 0);
            }
        }
    }
}
