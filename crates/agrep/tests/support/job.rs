//! Job-control stops of a child process, observed through the kernel rather than by polling.
use std::{sync::mpsc, time::Duration};

/// Whether child `pid` stops within `limit`; false when it exits first or the limit passes.
/// The report is left in place, so the caller's `Child` still reaps the process.
pub fn stops(pid: u32, limit: Duration) -> bool {
    let (sender, receiver) = mpsc::channel();
    // waitid has no timeout, so it blocks on this thread until the child stops or exits.
    std::thread::spawn(move || loop {
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        let options = libc::WSTOPPED | libc::WEXITED | libc::WNOWAIT;
        if unsafe { libc::waitid(libc::P_PID, pid, &mut info, options) } == 0 {
            let _ = sender.send(info.si_code == libc::CLD_STOPPED);
            return;
        }
        if std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
            return;
        }
    });
    receiver.recv_timeout(limit).unwrap_or(false)
}
