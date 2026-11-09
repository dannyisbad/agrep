//! The configured post-index command. Like 0.4.9's, it runs under a time limit with its
//! output kept apart, and its failure is reported without failing the index that ran it.
use crate::data::{self, Config, DataRoot};
use anyhow::Result;
use std::{
    fs::File,
    path::PathBuf,
    process::{Command, ExitStatus, Stdio},
    sync::Mutex,
    time::{Duration, Instant},
};

/// 0.4.9's cap for the user's post-index command (indexer.py HOOK_TIMEOUT_S).
pub const TIME_LIMIT: Duration = Duration::from_secs(600);
/// How long a stopped command's processes get to exit after TERM before they are killed.
const GRACE: Duration = Duration::from_millis(1500);

/// The configured command; `off` in any letter case, or a blank value, disables it.
pub fn command(config: &Config) -> Option<&str> {
    config.post_index.as_deref().filter(|raw| {
        let raw = raw.trim();
        !raw.is_empty() && !raw.eq_ignore_ascii_case("off")
    })
}

/// Where the last run's stdout and stderr go.
pub fn output_path(root: &DataRoot) -> PathBuf {
    root.path().join("log").join("post_index.log")
}

#[derive(Debug)]
pub(super) enum Outcome {
    Succeeded,
    Failed(ExitStatus),
    TimedOut,
    /// agrep got this signal while the command ran.
    Interrupted(i32),
}

/// Runs the command, then reports a failure on its own stderr line.
pub(super) fn run_and_report(root: &DataRoot, command: &str) {
    let path = output_path(root);
    let output = open_output(root);
    let shown = crate::render::safe(&path.display().to_string());
    let outcome = run(command, output.as_ref().ok(), TIME_LIMIT);
    let kept = match &output {
        Ok(_) => format!("its output is in {shown}"),
        Err(e) => format!(
            "its output was discarded: {}",
            crate::render::safe(&format!("{e:#}"))
        ),
    };
    match outcome {
        Ok(Outcome::Succeeded) => {}
        Ok(Outcome::Failed(status)) => crate::errln!(
            "post_index command failed ({}); the index is complete; {kept}",
            describe(status)
        ),
        Ok(Outcome::TimedOut) => crate::errln!(
            "post_index command stopped after {} minutes; the index is complete; {kept}",
            TIME_LIMIT.as_secs() / 60
        ),
        Ok(Outcome::Interrupted(signal)) => {
            wait::resend(signal);
            crate::errln!(
                "post_index command stopped by signal {signal}; the index is complete; {kept}"
            )
        }
        Err(e) => crate::errln!(
            "post_index command could not start: {}; the index is complete",
            crate::render::safe(&format!("{e:#}"))
        ),
    }
}

/// The same words on every platform: std's rendering differs between Unix and Windows.
fn describe(status: ExitStatus) -> String {
    if let Some(code) = status.code() {
        return format!("exit status {code}");
    }
    #[cfg(unix)]
    if let Some(signal) = std::os::unix::process::ExitStatusExt::signal(&status) {
        return format!("killed by signal {signal}");
    }
    status.to_string()
}

fn open_output(root: &DataRoot) -> Result<File> {
    if data::read_only() {
        anyhow::bail!("data directory is read-only");
    }
    data::private_dir(&root.path().join("log"))?;
    Ok(File::create(output_path(root))?)
}

/// Runs `command` through the platform shell with stdin closed and stdout and stderr in
/// `output`. Past `limit`, or when agrep gets INT, TERM or HUP, the command and everything
/// it started are stopped: it runs in its own process group, which a terminal's Ctrl-C does
/// not reach.
pub(super) fn run(command: &str, output: Option<&File>, limit: Duration) -> Result<Outcome> {
    // The signal handling is process-wide.
    static RUNNING: Mutex<()> = Mutex::new(());
    let _running = RUNNING.lock().unwrap_or_else(|e| e.into_inner());
    let mut shell = shell(command);
    shell.stdin(Stdio::null());
    match output {
        Some(file) => {
            shell.stdout(file.try_clone()?);
            shell.stderr(file.try_clone()?);
        }
        None => {
            shell.stdout(Stdio::null());
            shell.stderr(Stdio::null());
        }
    }
    let mut events = wait::Events::catch()?;
    let mut child = shell.spawn()?;
    events.watch(&child);
    let stopped = match events.next(limit) {
        Some(Event::Exited) => None,
        Some(Event::Signal(signal)) => Some(Outcome::Interrupted(signal)),
        None => Some(Outcome::TimedOut),
    };
    match stopped {
        Some(Outcome::Interrupted(signal)) => stop(child.id(), &events, Some(signal)),
        Some(_) => stop(child.id(), &events, None),
        None => {}
    }
    let status = child.wait()?;
    let signal = events.signal();
    drop(events);
    Ok(match stopped {
        Some(outcome) => outcome,
        None if signal != 0 => Outcome::Interrupted(signal),
        None if status.success() => Outcome::Succeeded,
        None => Outcome::Failed(status),
    })
}

/// Passes the command's process group the signal agrep got, or TERM past the time limit,
/// as 0.4.9 did; kills what is left after the grace period, and waits for the command
/// itself to exit.
fn stop(pid: u32, events: &wait::Events, signal: Option<i32>) {
    terminate(pid, signal);
    let deadline = Instant::now() + GRACE;
    let mut exited = false;
    while !exited {
        match events.next(deadline.saturating_duration_since(Instant::now())) {
            Some(Event::Exited) => exited = true,
            Some(Event::Signal(_)) => {}
            None => break,
        }
    }
    // A process the group's leader started may outlive it, so Unix kills the group anyway.
    if cfg!(unix) || !exited {
        kill_tree(pid);
    }
    while !exited {
        exited = matches!(events.next(Duration::MAX), Some(Event::Exited));
    }
}

/// What the wait for the command sees: its exit or, on Unix, a signal to agrep.
#[cfg_attr(windows, allow(dead_code))]
enum Event {
    Exited,
    Signal(i32),
}

#[cfg(not(windows))]
fn shell(command: &str) -> Command {
    use std::os::unix::process::CommandExt;
    let mut shell = Command::new("sh");
    shell.args(["-c", command]);
    // Its own process group, so a time-out reaches whatever the command started.
    shell.process_group(0);
    shell
}
#[cfg(windows)]
fn shell(command: &str) -> Command {
    use std::os::windows::process::CommandExt;
    let mut shell = Command::new("cmd");
    // std quotes arguments for the C runtime, escaping inner quotes in a form cmd.exe does
    // not parse; /S makes cmd strip exactly the outer quotes, so the line arrives verbatim.
    shell
        .args(["/D", "/S", "/C"])
        .raw_arg(format!("\"{command}\""));
    shell
}

// Signalling the group through the command's process ID is safe until the command is
// reaped, which `run` does only after `stop`.
#[cfg(not(windows))]
fn terminate(pid: u32, signal: Option<i32>) {
    unsafe {
        libc::kill(-(pid as i32), signal.unwrap_or(libc::SIGTERM));
    }
}
#[cfg(not(windows))]
fn kill_tree(pid: u32) {
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
}
/// cmd.exe has no polite way to end a console process tree.
#[cfg(windows)]
fn terminate(pid: u32, _: Option<i32>) {
    kill_tree(pid);
}
#[cfg(windows)]
fn kill_tree(pid: u32) {
    let _ = Command::new("taskkill")
        .args(["/T", "/F", "/PID", &pid.to_string()])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(not(windows))]
mod wait {
    use super::Event;
    use anyhow::Result;
    use std::{
        process::Child,
        sync::{
            atomic::{AtomicI32, Ordering},
            OnceLock,
        },
        time::Duration,
    };

    static SIGNAL: AtomicI32 = AtomicI32::new(0);
    static WAKE: AtomicI32 = AtomicI32::new(-1);

    extern "C" fn note(signal: i32) {
        let _ = SIGNAL.compare_exchange(0, signal, Ordering::SeqCst, Ordering::SeqCst);
        wake(signal as u8);
    }
    /// Byte 0 reports the command's exit, any other a signal.
    fn wake(byte: u8) {
        let fd = WAKE.load(Ordering::SeqCst);
        if fd >= 0 {
            unsafe {
                libc::write(fd, (&byte as *const u8).cast(), 1);
            }
        }
    }
    /// The wake pipe's read end. It is never closed: a handler running on another thread
    /// may still write to it after its signal action is restored.
    fn pipe() -> Result<i32> {
        static READ: OnceLock<i32> = OnceLock::new();
        if let Some(read) = READ.get() {
            return Ok(*read);
        }
        let mut fds = [0; 2];
        if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        for fd in fds {
            unsafe {
                libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
                libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK);
            }
        }
        WAKE.store(fds[1], Ordering::SeqCst);
        Ok(*READ.get_or_init(|| fds[0]))
    }

    pub(super) struct Events {
        read: i32,
        old: Vec<(i32, libc::sigaction)>,
    }
    impl Events {
        /// From here until it is dropped, INT, TERM and HUP are noted instead of ending
        /// agrep at once; one the shell had agrep ignore stays ignored.
        pub(super) fn catch() -> Result<Self> {
            let read = pipe()?;
            while unsafe { libc::read(read, [0u8].as_mut_ptr().cast(), 1) } > 0 {}
            SIGNAL.store(0, Ordering::SeqCst);
            let mut events = Self { read, old: vec![] };
            for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
                let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
                if unsafe { libc::sigaction(signal, std::ptr::null(), &mut old) } != 0 {
                    return Err(std::io::Error::last_os_error().into());
                }
                if old.sa_sigaction == libc::SIG_IGN {
                    continue;
                }
                let mut new: libc::sigaction = unsafe { std::mem::zeroed() };
                new.sa_sigaction = note as *const () as usize;
                new.sa_flags = libc::SA_RESTART;
                unsafe { libc::sigemptyset(&mut new.sa_mask) };
                if unsafe { libc::sigaction(signal, &new, std::ptr::null_mut()) } != 0 {
                    return Err(std::io::Error::last_os_error().into());
                }
                events.old.push((signal, old));
            }
            Ok(events)
        }
        /// Reports the command's exit without reaping it, so that its process ID stays its
        /// group's until `stop` is done with the group.
        pub(super) fn watch(&mut self, child: &Child) {
            let pid = child.id();
            std::thread::spawn(move || {
                let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
                let flags = libc::WEXITED | libc::WNOWAIT;
                while unsafe { libc::waitid(libc::P_PID, pid, &mut info, flags) } != 0
                    && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
                {
                }
                wake(0);
            });
        }
        /// The next event within `timeout`.
        pub(super) fn next(&self, timeout: Duration) -> Option<Event> {
            let deadline = std::time::Instant::now().checked_add(timeout);
            loop {
                let mut byte = 0u8;
                if unsafe { libc::read(self.read, (&mut byte as *mut u8).cast(), 1) } == 1 {
                    return Some(match byte {
                        0 => Event::Exited,
                        signal => Event::Signal(signal.into()),
                    });
                }
                let millis = match deadline {
                    None => -1,
                    Some(deadline) => {
                        let left = deadline.saturating_duration_since(std::time::Instant::now());
                        if left.is_zero() {
                            return None;
                        }
                        // Rounded up, so that a poll never returns before the deadline.
                        left.as_nanos().div_ceil(1_000_000).min(i32::MAX as u128) as i32
                    }
                };
                let mut poll = libc::pollfd {
                    fd: self.read,
                    events: libc::POLLIN,
                    revents: 0,
                };
                unsafe { libc::poll(&mut poll, 1, millis) };
            }
        }
        /// The first signal noted since `catch`, or 0.
        pub(super) fn signal(&self) -> i32 {
            SIGNAL.load(Ordering::SeqCst)
        }
    }
    impl Drop for Events {
        fn drop(&mut self) {
            for (signal, old) in &self.old {
                unsafe {
                    libc::sigaction(*signal, old, std::ptr::null_mut());
                }
            }
        }
    }
    /// Takes `signal` again under the action agrep had before, which ends agrep with the
    /// status a shell expects of it.
    pub(super) fn resend(signal: i32) {
        unsafe {
            libc::raise(signal);
        }
    }
}
#[cfg(windows)]
mod wait {
    use super::Event;
    use anyhow::Result;
    use std::{
        process::Child,
        sync::mpsc::{channel, Receiver, Sender},
        time::Duration,
    };

    pub(super) struct Events {
        send: Option<Sender<()>>,
        receive: Receiver<()>,
    }
    impl Events {
        pub(super) fn catch() -> Result<Self> {
            let (send, receive) = channel();
            Ok(Self {
                send: Some(send),
                receive,
            })
        }
        /// Reports the command's exit without reaping it; the caller waits for this report
        /// before it drops the child, which closes the handle.
        pub(super) fn watch(&mut self, child: &Child) {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::System::Threading::{WaitForSingleObject, INFINITE};
            let handle = child.as_raw_handle() as usize;
            let send = self.send.take().expect("one command per wait");
            std::thread::spawn(move || {
                unsafe { WaitForSingleObject(handle as _, INFINITE) };
                let _ = send.send(());
            });
        }
        pub(super) fn next(&self, timeout: Duration) -> Option<Event> {
            self.receive
                .recv_timeout(timeout)
                .ok()
                .map(|()| Event::Exited)
        }
        pub(super) fn signal(&self) -> i32 {
            0
        }
    }
    pub(super) fn resend(_: i32) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn off_in_any_case_or_blank_disables_the_command() {
        let config = |value: Option<&str>| Config {
            post_index: value.map(str::to_owned),
            ..Default::default()
        };
        for disabled in [
            None,
            Some(""),
            Some("  "),
            Some("off"),
            Some("OFF"),
            Some(" Off "),
        ] {
            assert_eq!(command(&config(disabled)), None, "{disabled:?}");
        }
        assert_eq!(command(&config(Some("echo off"))), Some("echo off"));
    }

    // The output pipe reaches end of file only once every process holding it has exited,
    // so a background child the time-out missed would hold the read for 30 seconds. The
    // second Unix command ignores TERM, so only the kill after the grace period ends it.
    #[test]
    fn a_command_past_its_limit_is_killed_with_everything_it_started() {
        use std::io::Read;
        let commands: &[&str] = if cfg!(windows) {
            &["start /B ping -n 31 127.0.0.1 & ping -n 31 127.0.0.1"]
        } else {
            &["sleep 30 & sleep 30", "trap '' TERM; sleep 30 & sleep 30"]
        };
        for command in commands {
            let (mut read, write) = std::io::pipe().unwrap();
            #[cfg(not(windows))]
            let write = File::from(std::os::fd::OwnedFd::from(write));
            #[cfg(windows)]
            let write = File::from(std::os::windows::io::OwnedHandle::from(write));
            let started = std::time::Instant::now();
            let outcome = run(command, Some(&write), Duration::from_millis(300)).unwrap();
            drop(write);
            read.read_to_end(&mut Vec::new()).unwrap();
            assert!(
                matches!(outcome, Outcome::TimedOut),
                "{command}: {outcome:?}"
            );
            assert!(started.elapsed() < Duration::from_secs(20), "{command}");
        }
    }

    #[test]
    fn a_failing_command_reports_its_status_and_keeps_its_output() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("out");
        let file = File::create(&path).unwrap();
        let outcome = run("echo said && exit 3", Some(&file), TIME_LIMIT).unwrap();
        assert!(
            matches!(outcome, Outcome::Failed(status) if status.code() == Some(3)),
            "{outcome:?}"
        );
        assert!(std::fs::read_to_string(path).unwrap().starts_with("said"));
    }
}
