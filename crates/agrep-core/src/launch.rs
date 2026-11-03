//! Native agent commands and owned foreground processes.
use crate::history::{self, Session};
use anyhow::{bail, Context, Result};
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    process::Command,
};
pub fn capability(agent: &str) -> Result<(&'static str, &'static str)> {
    match history::normalize_agent(agent) {
        "claude" => Ok(("claude", "--resume")),
        "codex" => Ok(("codex", "resume")),
        "opencode" => Ok(("opencode", "--session")),
        "antigravity" => Ok(("agy", "--conversation")),
        "pi" => Ok(("pi", "-r")),
        "cline" | "cursor" => {
            bail!("{agent} sessions are editor-owned and have no standalone resume command")
        }
        "kimi" | "gemini" | "crush" => bail!("{agent} has no verified native resume/run command"),
        _ => bail!("unknown agent: {agent}"),
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
    #[cfg(unix)]
    use std::os::unix::process::CommandExt;
    #[cfg(unix)]
    if unsafe { libc::isatty(0) == 0 || libc::isatty(1) == 1 } {
        command.process_group(0);
    }
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
    let mut signals = unix::Signals::install()?;
    let mut child = command
        .spawn()
        .with_context(|| format!("could not launch {}", exe.display()))?;
    #[cfg(unix)]
    signals.foreground(child.id());
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
    use std::sync::atomic::{AtomicI32, Ordering};
    static PID: AtomicI32 = AtomicI32::new(0);
    static SIGNAL: AtomicI32 = AtomicI32::new(0);
    extern "C" fn relay(sig: i32) {
        let _ = SIGNAL.compare_exchange(0, sig, Ordering::SeqCst, Ordering::SeqCst);
        let pid = PID.load(Ordering::SeqCst);
        if pid > 0 {
            unsafe {
                if libc::kill(-pid, sig) != 0 {
                    libc::kill(pid, sig);
                }
            }
        }
    }
    pub struct Signals {
        old: Vec<(i32, libc::sigaction)>,
        pgrp: i32,
        tty: bool,
        termios: Option<libc::termios>,
    }
    impl Signals {
        pub fn install() -> Result<Self> {
            let mut s = Self {
                old: vec![],
                pgrp: unsafe { libc::tcgetpgrp(0) },
                tty: unsafe {
                    libc::isatty(0) == 1
                        && libc::isatty(1) == 1
                        && libc::tcgetpgrp(0) == libc::getpgrp()
                },
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
            for sig in [
                libc::SIGINT,
                libc::SIGTERM,
                libc::SIGHUP,
                libc::SIGQUIT,
                libc::SIGTTOU,
            ] {
                let mut new: libc::sigaction = unsafe { std::mem::zeroed() };
                new.sa_sigaction = if sig == libc::SIGTTOU {
                    libc::SIG_IGN
                } else {
                    relay as *const () as usize
                };
                unsafe { libc::sigemptyset(&mut new.sa_mask) };
                let mut old = unsafe { std::mem::zeroed() };
                if unsafe { libc::sigaction(sig, &new, &mut old) } != 0 {
                    return Err(std::io::Error::last_os_error().into());
                }
                s.old.push((sig, old));
            }
            Ok(s)
        }
        pub fn foreground(&mut self, pid: u32) {
            PID.store(pid as i32, Ordering::SeqCst);
            if self.tty {
                unsafe {
                    libc::tcsetpgrp(0, pid as i32);
                    // A fast reader may have received SIGTTIN before the parent handed over the terminal.
                    libc::kill(-(pid as i32), libc::SIGCONT);
                }
            }
            let sig = self.signal();
            if sig != 0 {
                unsafe {
                    libc::kill(-(pid as i32), sig);
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
                        continue;
                    }
                    return Err(error.into());
                }
                if info.si_code == libc::CLD_STOPPED {
                    let mut status = 0;
                    unsafe {
                        libc::waitpid(pid as i32, &mut status, libc::WUNTRACED);
                        if self.tty {
                            libc::tcsetpgrp(0, self.pgrp);
                        }
                        libc::raise(libc::SIGTSTP);
                    }
                    self.foreground(pid);
                    unsafe {
                        libc::kill(-(pid as i32), libc::SIGCONT);
                    }
                } else {
                    return Ok(());
                }
            }
        }
        pub fn finish(&mut self, pid: u32) {
            if self.tty && self.pgrp > 0 {
                unsafe {
                    libc::tcsetpgrp(0, self.pgrp);
                }
            }
            // wait_for_exit uses WNOWAIT: this child still reserves the PID,
            // even on systems where getpgid no longer resolves an exited leader.
            unsafe {
                libc::kill(-(pid as i32), libc::SIGTERM);
            }
            std::thread::sleep(std::time::Duration::from_millis(1500));
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
            }
            if let Some(t) = self.termios.as_ref() {
                unsafe {
                    libc::tcsetattr(0, libc::TCSANOW, t);
                }
            }

            if self.tty && self.pgrp > 0 {
                unsafe {
                    libc::tcsetpgrp(0, self.pgrp);
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
