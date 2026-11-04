//! Waiting for a process to exit on the operating system's exit notification.
use std::{io, time::Duration};

/// Waits up to `limit` for process `pid` to exit; true once it has. A process that is
/// already gone counts as exited. Nothing is reaped, so a child keeps its PID meanwhile.
#[cfg(target_os = "macos")]
pub fn wait(pid: u32, limit: Duration) -> io::Result<bool> {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    let kq = unsafe { libc::kqueue() };
    if kq < 0 {
        return Err(io::Error::last_os_error());
    }
    let kq = unsafe { OwnedFd::from_raw_fd(kq) };
    let change = libc::kevent {
        ident: pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_ADD | libc::EV_ONESHOT,
        fflags: libc::NOTE_EXIT,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    let mut event = change;
    let timeout = libc::timespec {
        tv_sec: limit.as_secs() as libc::time_t,
        tv_nsec: limit.subsec_nanos() as libc::c_long,
    };
    let n = unsafe { libc::kevent(kq.as_raw_fd(), &change, 1, &mut event, 1, &timeout) };
    let error = if n < 0 {
        io::Error::last_os_error()
    } else if n == 1 && event.flags & libc::EV_ERROR != 0 {
        io::Error::from_raw_os_error(event.data as i32)
    } else {
        return Ok(n == 1);
    };
    // The process was gone before it could be watched.
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(true)
    } else {
        Err(error)
    }
}

/// Waits up to `limit` for process `pid` to exit; true once it has. A process that is
/// already gone counts as exited. Nothing is reaped, so a child keeps its PID meanwhile.
#[cfg(target_os = "linux")]
pub fn wait(pid: u32, limit: Duration) -> io::Result<bool> {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::pid_t, 0) };
    if fd < 0 {
        let error = io::Error::last_os_error();
        // The process was gone before it could be watched.
        return if error.raw_os_error() == Some(libc::ESRCH) {
            Ok(true)
        } else {
            Err(error)
        };
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd as libc::c_int) };
    let mut poll = libc::pollfd {
        fd: fd.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let millis = limit.as_millis().min(i32::MAX as u128) as libc::c_int;
    match unsafe { libc::poll(&mut poll, 1, millis) } {
        n if n < 0 => Err(io::Error::last_os_error()),
        n => Ok(n == 1),
    }
}

/// Waits up to `limit` for process `pid` to exit; true once it has. A process that is
/// already gone counts as exited. An open handle keeps a child's ID from being reused.
#[cfg(windows)]
pub fn wait(pid: u32, limit: Duration) -> io::Result<bool> {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, ERROR_INVALID_PARAMETER, WAIT_OBJECT_0, WAIT_TIMEOUT},
        System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE},
    };
    let process = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if process.is_null() {
        let error = io::Error::last_os_error();
        // No process has this ID any more.
        return if error.raw_os_error() == Some(ERROR_INVALID_PARAMETER as i32) {
            Ok(true)
        } else {
            Err(error)
        };
    }
    let millis = limit.as_millis().min(u32::MAX as u128 - 1) as u32;
    let result = unsafe { WaitForSingleObject(process, millis) };
    let error = io::Error::last_os_error();
    unsafe {
        CloseHandle(process);
    }
    match result {
        WAIT_OBJECT_0 => Ok(true),
        WAIT_TIMEOUT => Ok(false),
        _ => Err(error),
    }
}
