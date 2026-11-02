//! RAII terminal ownership and signal exits for foreground live commands.
use anyhow::Result;
use crossterm::{
    cursor::Hide,
    event::{self, EnableBracketedPaste, Event, KeyCode, KeyModifiers},
    execute,
    terminal::{self, EnterAlternateScreen},
};
use std::{
    io,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};

#[cfg(windows)]
static CONSOLE_SIGNAL: AtomicUsize = AtomicUsize::new(0);
#[cfg(windows)]
unsafe extern "system" fn console_signal(kind: u32) -> i32 {
    use windows_sys::Win32::System::Console::{CTRL_BREAK_EVENT, CTRL_C_EVENT};
    let signal = match kind {
        CTRL_C_EVENT => 2,
        CTRL_BREAK_EVENT => 1000,
        _ => 15,
    };
    let _ = CONSOLE_SIGNAL.compare_exchange(0, signal, Ordering::SeqCst, Ordering::SeqCst);
    1
}
pub struct Signals {
    value: Arc<AtomicUsize>,
    #[cfg(unix)]
    ids: Vec<signal_hook::SigId>,
}
impl Signals {
    pub fn new() -> Result<Self> {
        Self::with_job_control(false)
    }
    pub fn board() -> Result<Self> {
        Self::with_job_control(true)
    }
    fn with_job_control(_job_control: bool) -> Result<Self> {
        let value = Arc::new(AtomicUsize::new(0));
        #[cfg(unix)]
        {
            let mut ids = vec![];
            let mut signals = vec![libc::SIGINT, libc::SIGTERM, libc::SIGHUP, libc::SIGQUIT];
            if _job_control {
                signals.push(libc::SIGTSTP);
            }
            for signal in signals {
                let v = value.clone();
                // SAFETY: the handler performs only a lock-free atomic operation.
                ids.push(unsafe {
                    signal_hook::low_level::register(signal, move || {
                        let _ = v.compare_exchange(
                            0,
                            signal as usize,
                            Ordering::SeqCst,
                            Ordering::SeqCst,
                        );
                    })
                }?);
            }
            Ok(Self { value, ids })
        }
        #[cfg(windows)]
        {
            CONSOLE_SIGNAL.store(0, Ordering::SeqCst);
            if unsafe {
                windows_sys::Win32::System::Console::SetConsoleCtrlHandler(Some(console_signal), 1)
            } == 0
            {
                return Err(io::Error::last_os_error().into());
            }
        }
        #[cfg(not(unix))]
        {
            Ok(Self { value })
        }
    }
    pub fn pending(&self) -> usize {
        #[cfg(windows)]
        {
            let v = CONSOLE_SIGNAL.load(Ordering::SeqCst);
            if v != 0 {
                return v;
            }
        }
        self.value.load(Ordering::SeqCst)
    }
    pub fn exit_code(&self) -> i32 {
        if self.pending() == 1000 {
            3
        } else {
            128 + self.pending() as i32
        }
    }
    pub fn take(&self) -> usize {
        #[cfg(windows)]
        {
            CONSOLE_SIGNAL.store(0, Ordering::SeqCst);
        }
        self.value.swap(0, Ordering::SeqCst)
    }
}
impl Drop for Signals {
    fn drop(&mut self) {
        #[cfg(windows)]
        unsafe {
            windows_sys::Win32::System::Console::SetConsoleCtrlHandler(Some(console_signal), 0);
        }
        #[cfg(unix)]
        for id in &self.ids {
            signal_hook::low_level::unregister(*id);
        }
    }
}

pub struct Screen {
    active: bool,
}
impl Screen {
    pub fn enter() -> Result<Self> {
        let mut screen = Self { active: false };
        screen.activate()?;
        Ok(screen)
    }
    fn activate(&mut self) -> Result<()> {
        terminal::enable_raw_mode()?;
        self.active = true;
        // Keep terminal-generated job-control and quit signals in raw mode.
        #[cfg(unix)]
        unsafe {
            let mut mode = std::mem::zeroed();
            if libc::tcgetattr(libc::STDIN_FILENO, &mut mode) != 0 {
                self.restore();
                return Err(io::Error::last_os_error().into());
            }
            mode.c_lflag |= libc::ISIG;
            if libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, &mode) != 0 {
                self.restore();
                return Err(io::Error::last_os_error().into());
            }
        }
        if let Err(e) = execute!(
            io::stdout(),
            EnterAlternateScreen,
            EnableBracketedPaste,
            Hide
        ) {
            self.restore();
            return Err(e.into());
        }
        Ok(())
    }
    fn restore(&mut self) {
        if !self.active {
            return;
        }
        self.active = false;
        #[cfg(unix)]
        unsafe {
            libc::tcflush(libc::STDIN_FILENO, libc::TCIFLUSH);
        }
        let _ = terminal::disable_raw_mode();
        #[cfg(unix)]
        unsafe {
            libc::tcflush(libc::STDIN_FILENO, libc::TCIFLUSH);
        }
        let _ = write_terminal(
            b"\x1b[0m\x1b[?25h\x1b[?2004l\x1b[?1049l",
            Duration::from_secs(5),
            None,
        );
    }
    pub fn signal(&mut self, signals: &Signals) -> Result<Option<i32>> {
        let signal = signals.pending();
        if signal == 0 {
            return Ok(None);
        }
        #[cfg(unix)]
        if signal == libc::SIGTSTP as usize {
            signals.take();
            self.restore();
            // SIGSTOP is uncatchable; the controlling shell resumes us with SIGCONT.
            unsafe {
                libc::raise(libc::SIGSTOP);
            }
            self.activate()?;
            return Ok(None);
        }
        self.restore();
        Ok(Some(signals.exit_code()))
    }
    pub fn draw(&self, frame: &str, signals: &Signals) -> Result<()> {
        let frame = format!("\x1b[H\x1b[2J{}", frame.replace('\n', "\r\n"));
        write_terminal(frame.as_bytes(), Duration::from_millis(100), Some(signals))?;
        Ok(())
    }
    pub fn key(&self, wait: Duration) -> Result<Option<String>> {
        if !event::poll(wait)? {
            return Ok(None);
        }
        Ok(match event::read()? {
            Event::Paste(_) => Some("paste".into()),
            Event::Key(k) if k.kind != event::KeyEventKind::Release => Some(match k.code {
                KeyCode::Char('c') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                    "ctrl-c".into()
                }
                KeyCode::Char('d') if k.modifiers.contains(KeyModifiers::CONTROL) => "eof".into(),
                KeyCode::Char(c) => c.to_string(),
                KeyCode::Up => "up".into(),
                KeyCode::Down => "down".into(),
                KeyCode::Enter => "enter".into(),
                KeyCode::Esc => "esc".into(),
                _ => String::new(),
            }),
            _ => None,
        })
    }
    pub fn size() -> (usize, usize) {
        terminal::size()
            .ok()
            .filter(|(w, h)| *w > 0 && *h > 0)
            .map(|(w, h)| (w as usize, h as usize))
            .unwrap_or((110, 34))
    }
}
impl Drop for Screen {
    fn drop(&mut self) {
        self.restore();
    }
}
#[cfg(unix)]
fn write_terminal(bytes: &[u8], timeout: Duration, signals: Option<&Signals>) -> io::Result<()> {
    let start = std::time::Instant::now();
    // A separate open file description keeps O_NONBLOCK off the shell's stdout.
    // Polling and bounded writes allow terminal restoration even on a full PTY.
    use std::os::fd::{AsRawFd, FromRawFd};
    let fd = unsafe {
        libc::open(
            c"/dev/tty".as_ptr(),
            libc::O_WRONLY | libc::O_NONBLOCK | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let terminal = unsafe { std::fs::File::from_raw_fd(fd) };
    let fd = terminal.as_raw_fd();
    let mut offset = 0;
    while offset < bytes.len() {
        if signals.is_some_and(|s| s.pending() != 0) || start.elapsed() >= timeout {
            return Ok(());
        }
        let mut poll = libc::pollfd {
            fd,
            events: libc::POLLOUT,
            revents: 0,
        };
        if unsafe { libc::poll(&mut poll, 1, 20) } <= 0 {
            continue;
        }
        let n = unsafe { libc::write(fd, bytes[offset..].as_ptr().cast(), bytes.len() - offset) };
        if n > 0 {
            offset += n as usize;
            continue;
        }
        let e = io::Error::last_os_error();
        if !matches!(
            e.kind(),
            io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
        ) {
            return Err(e);
        }
    }
    Ok(())
}
#[cfg(not(unix))]
fn write_terminal(bytes: &[u8], _: Duration, _: Option<&Signals>) -> io::Result<()> {
    io::stdout().write_all(bytes)
}

#[cfg(not(unix))]
use std::io::Write;

/// Output consumers can stop a JSON stream without turning that into a command failure.
pub fn output_closed(error: &anyhow::Error) -> bool {
    error.chain().any(|e| {
        e.downcast_ref::<io::Error>()
            .is_some_and(|e| e.kind() == io::ErrorKind::BrokenPipe)
            || e.downcast_ref::<serde_json::Error>()
                .is_some_and(|e| e.io_error_kind() == Some(io::ErrorKind::BrokenPipe))
    })
}
/// Avoid starting a census or waiting for another event after a pipe consumer exits.
pub fn output_is_closed() -> bool {
    #[cfg(unix)]
    unsafe {
        let mut p = libc::pollfd {
            fd: libc::STDOUT_FILENO,
            events: libc::POLLOUT,
            revents: 0,
        };
        libc::poll(&mut p, 1, 0) > 0
            && p.revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0
    }
    #[cfg(not(unix))]
    {
        false
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn closed_json_output_is_normal_termination() {
        let e = anyhow::Error::new(io::Error::new(io::ErrorKind::BrokenPipe, "closed"));
        assert!(output_closed(&e));
        assert!(!output_closed(&anyhow::anyhow!("parse failed")));
    }
}
