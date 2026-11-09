use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Command, Stdio},
    time::Duration,
};
use tempfile::TempDir;
#[path = "../examples/support/exit.rs"]
mod exit;
#[cfg(unix)]
#[path = "support/job.rs"]
mod job;
const BIN: &str = env!("CARGO_BIN_EXE_agrep");
fn cli(home: &Path, data: &Path) -> Command {
    let mut c = Command::new(BIN);
    c.env_clear()
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data);
    c
}
fn fixture(home: &Path) -> std::path::PathBuf {
    let p = home.join(".claude/projects/demo/root.jsonl");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    let time = chrono::Utc::now().to_rfc3339();
    fs::write(&p,format!("{}\n{}\n",json!({"type":"user","sessionId":"root-one","cwd":"/work/demo","timestamp":time,"message":{"content":"build example"}}),json!({"type":"assistant","sessionId":"root-one","timestamp":time,"message":{"content":[{"type":"text","text":"finished"}],"stop_reason":"end_turn"}}))).unwrap();
    p
}
#[test]
fn board_snapshot_and_tail_scoping() {
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let data = t.path().join("data");
    fixture(&home);
    let out = cli(&home, &data)
        .args(["board", "--json", "--wait", "5"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let board: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(board["kind"], "agrep-board-snapshot");
    assert_eq!(board["snapshot_complete"], true);
    assert_eq!(board["sessions"][0]["session"], "root-one");
    assert_eq!(board["sessions"][0]["working"], false);
    assert_eq!(board["completeness"]["matched"], 1);
    let out = cli(&home, &data)
        .args(["tail", "--snapshot", "--agent", "codex"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let snap: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(snap["type"], "snapshot");
    assert_eq!(snap["counts"]["sessions"], 0);
    assert!(snap.get("last_err").is_none());
    let out = cli(&home, &data)
        .args(["board", "--json", "--session", "missing", "--wait", "5"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1));
    for args in [
        vec!["board", "--json", "--expanded"],
        vec!["board", "--max", "0"],
        vec!["board", "--wait", "NaN"],
        vec!["tail", "--snapshot", "--events", "all"],
        vec!["tail", "--agent", "crush"],
        vec!["tail", "--events", "all,user"],
    ] {
        assert_eq!(
            cli(&home, &data)
                .args(&args)
                .output()
                .unwrap()
                .status
                .code(),
            Some(2),
            "{args:?}"
        );
    }
    assert!(!data.exists(), "live views must never initialize an index");
}
#[test]
fn ready_precedes_delta_and_filters_events() {
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let data = t.path().join("data");
    let path = fixture(&home);
    let mut child = cli(&home, &data)
        .args([
            "tail",
            "--agent",
            "claude",
            "--session",
            "root-one",
            "--events",
            "all",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let stdout = child.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    let first: Value =
        serde_json::from_str(&rx.recv_timeout(Duration::from_secs(10)).unwrap().unwrap()).unwrap();
    assert_eq!(first["type"], "tail_ready");
    let mut f = fs::OpenOptions::new().append(true).open(path).unwrap();
    let time = chrono::Utc::now().to_rfc3339();
    let event=json!({"type":"user","sessionId":"root-one","timestamp":time,"message":{"content":"new event"}}).to_string();
    f.write_all(event.as_bytes()).unwrap();
    f.flush().unwrap();
    f.write_all(b"\n").unwrap();
    f.flush().unwrap();
    let next: Value =
        serde_json::from_str(&rx.recv_timeout(Duration::from_secs(5)).unwrap().unwrap()).unwrap();
    assert_eq!(next["type"], "user");
    assert_eq!(next["text"], "new event");
    #[cfg(unix)]
    {
        unsafe {
            libc::kill(child.id() as i32, libc::SIGINT);
        }
        assert_eq!(child.wait().unwrap().code(), Some(130));
    }
    #[cfg(windows)]
    {
        child.kill().unwrap();
        child.wait().unwrap();
    }
    reader.join().unwrap();
}
#[cfg(unix)]
mod pty {
    use super::*;
    use std::{
        fs::File,
        io::Read,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::process::CommandExt,
        },
        time::Instant,
    };
    struct Terminal {
        master: Option<File>,
        _slave: File,
        before: libc::termios,
        child: std::process::Child,
    }
    impl Terminal {
        fn start(home: &Path, data: &Path) -> Self {
            Self::start_with(home, data, &[])
        }
        fn start_with(home: &Path, data: &Path, args: &[&str]) -> Self {
            let (mut master, mut slave) = (0, 0);
            unsafe {
                assert_eq!(
                    libc::openpty(
                        &mut master,
                        &mut slave,
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        std::ptr::null_mut()
                    ),
                    0
                );
            }
            unsafe {
                libc::fcntl(master, libc::F_SETFD, libc::FD_CLOEXEC);
                libc::fcntl(slave, libc::F_SETFD, libc::FD_CLOEXEC);
            }
            let master = unsafe { File::from_raw_fd(master) };
            let slave = unsafe { File::from_raw_fd(slave) };
            let mut before = unsafe { std::mem::zeroed() };
            unsafe {
                assert_eq!(libc::tcgetattr(slave.as_raw_fd(), &mut before), 0);
            }
            let mut c = cli(home, data);
            c.arg("board")
                .args(args)
                .stdin(slave.try_clone().unwrap())
                .stdout(slave.try_clone().unwrap())
                .stderr(slave.try_clone().unwrap());
            unsafe {
                c.pre_exec(|| {
                    if libc::setsid() < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    if libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
            let child = c.spawn().unwrap();
            Self {
                master: Some(master),
                _slave: slave,
                before,
                child,
            }
        }
        fn read_until(&mut self, needle: &[u8]) -> Vec<u8> {
            let deadline = Instant::now() + Duration::from_secs(60);
            let mut out = vec![];
            loop {
                let mut poll = libc::pollfd {
                    fd: self.master.as_ref().unwrap().as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                };
                let left = deadline.saturating_duration_since(Instant::now());
                match unsafe { libc::poll(&mut poll, 1, left.as_millis() as libc::c_int) } {
                    0 => panic!("missing pty output: {:?}", String::from_utf8_lossy(&out)),
                    n if n < 0 => panic!("pty poll: {}", std::io::Error::last_os_error()),
                    _ => {}
                }
                let mut b = [0; 4096];
                match self.master.as_mut().unwrap().read(&mut b) {
                    Ok(0) => panic!("pty closed: {:?}", String::from_utf8_lossy(&out)),
                    Ok(n) => {
                        out.extend_from_slice(&b[..n]);
                        if out.windows(needle.len()).any(|s| s == needle) {
                            return out;
                        }
                    }
                    Err(e) => panic!("pty read: {e}; {:?}", String::from_utf8_lossy(&out)),
                }
            }
        }
        fn finish(&mut self, code: i32) {
            self.read_until(b"\x1b[?1049l");
            let mut after = unsafe { std::mem::zeroed() };
            unsafe {
                assert_eq!(
                    libc::tcgetattr(self.master.as_ref().unwrap().as_raw_fd(), &mut after),
                    0
                )
            };
            assert_eq!(self.before.c_lflag, after.c_lflag);
            assert_eq!(self.before.c_iflag, after.c_iflag);
            assert_eq!(self.before.c_oflag, after.c_oflag);
            assert_eq!(self.before.c_cc, after.c_cc);
            self.master.take();
            if !exit::wait(self.child.id(), Duration::from_secs(10)).unwrap() {
                self.child.kill().unwrap();
                panic!("board did not exit")
            }
            assert_eq!(self.child.wait().unwrap().code(), Some(code));
        }
    }
    impl Drop for Terminal {
        fn drop(&mut self) {
            self.master.take();
            if self.child.try_wait().ok().flatten().is_none() {
                let _ = self.child.kill();
                let _ = self.child.wait();
            }
        }
    }
    #[test]
    fn paste_is_inert_and_terminal_restored() {
        let t = TempDir::new().unwrap();
        fixture(t.path());
        let mut term = Terminal::start(t.path(), &t.path().join("data"));
        term.read_until(b"finished");
        term.master
            .as_mut()
            .unwrap()
            .write_all(b"\x1b[200~qqq\ncq\x1b[201~")
            .unwrap();
        // A key after the paste must still be handled and redraw the expanded row.
        term.master.as_mut().unwrap().write_all(b"c").unwrap();
        term.read_until(b"@root-one");
        assert!(
            term.child.try_wait().unwrap().is_none(),
            "pasted q must not quit"
        );
        assert!(
            !t.path().join("data").exists(),
            "interactive reads must not initialize data"
        );
        term.master.as_mut().unwrap().write_all(b"q").unwrap();
        term.finish(0);
    }
    #[test]
    fn prefix_selector_focuses_and_stdout_flags_stay_blocking() {
        let t = TempDir::new().unwrap();
        fixture(t.path());
        let mut term =
            Terminal::start_with(t.path(), &t.path().join("data"), &["--session", "root-"]);
        term.read_until(b"finished");
        let flags = unsafe { libc::fcntl(term._slave.as_raw_fd(), libc::F_GETFL) };
        assert_eq!(flags & libc::O_NONBLOCK, 0);
        // Focus shows the session's actual id, unlike the collapsed list.
        term.master.as_mut().unwrap().write_all(b"c").unwrap();
        let frame = term.read_until(b"root-one");
        assert!(frame
            .windows(b"claude root-one".len())
            .any(|w| w == b"claude root-one"));
        term.master.as_mut().unwrap().write_all(b"q").unwrap();
        term.finish(0);
    }
    #[test]
    fn tail_keeps_default_suspension() {
        let t = TempDir::new().unwrap();
        fixture(t.path());
        let mut child = cli(t.path(), &t.path().join("data"))
            .env("PATH", "")
            .arg("tail")
            .process_group(0)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let mut line = String::new();
        let mut reader = BufReader::new(child.stdout.take().unwrap());
        reader.read_line(&mut line).unwrap();
        assert!(!line.is_empty());
        unsafe {
            libc::kill(child.id() as i32, libc::SIGTSTP);
        }
        let stopped = job::stops(child.id(), Duration::from_secs(5));
        unsafe {
            libc::kill(child.id() as i32, libc::SIGCONT);
            libc::kill(child.id() as i32, libc::SIGINT);
        }
        let status = child.wait().unwrap();
        assert!(stopped, "tail did not suspend");
        assert_eq!(status.code(), Some(130));
    }
    #[test]
    fn signal_and_suspend_restore() {
        for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP, libc::SIGQUIT] {
            let t = TempDir::new().unwrap();
            let mut term = Terminal::start(t.path(), &t.path().join("data"));
            term.read_until(b"\x1b[?2004h");
            unsafe {
                libc::kill(term.child.id() as i32, sig);
            }
            term.finish(128 + sig);
        }
        let t = TempDir::new().unwrap();
        let mut term = Terminal::start(t.path(), &t.path().join("data"));
        term.read_until(b"\x1b[?2004h");
        term.master.as_mut().unwrap().write_all(b"\x1a").unwrap();
        term.read_until(b"\x1b[?1049l");
        assert!(
            job::stops(term.child.id(), Duration::from_secs(5)),
            "board did not suspend"
        );
        unsafe {
            libc::kill(term.child.id() as i32, libc::SIGCONT);
        }
        term.read_until(b"\x1b[?2004h");
        term.master.as_mut().unwrap().write_all(b"q").unwrap();
        term.finish(0);
    }
}

#[test]
fn closed_tail_output_exits_successfully() {
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let path = fixture(&home);
    let mut child = cli(&home, &t.path().join("data"))
        .args(["tail", "--events", "all"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let stdout = child.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        tx.send((line, reader)).unwrap();
    });
    let (ready, pipe) = rx.recv_timeout(Duration::from_secs(10)).unwrap();
    assert!(ready.contains("tail_ready"));
    drop(pipe);
    reader.join().unwrap();
    let mut f = fs::OpenOptions::new().append(true).open(path).unwrap();
    writeln!(f,"{}",json!({"type":"user","sessionId":"root-one","timestamp":chrono::Utc::now().to_rfc3339(),"message":{"content":"next"}})).unwrap();
    if !exit::wait(child.id(), Duration::from_secs(5)).unwrap() {
        child.kill().unwrap();
        let _ = child.wait();
        panic!("tail did not stop on closed output")
    }
    assert!(child.wait().unwrap().success());
}

#[test]
fn closed_output_before_ready_exits_successfully() {
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    fs::create_dir_all(&home).unwrap();
    let mut child = cli(&home, &t.path().join("data"))
        .args(["tail"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    drop(child.stdout.take());
    if !exit::wait(child.id(), Duration::from_secs(5)).unwrap() {
        child.kill().unwrap();
        let output = child.wait_with_output().unwrap();
        panic!(
            "tail did not stop before ready: {}",
            String::from_utf8_lossy(&output.stderr)
        )
    }
    assert!(child.wait().unwrap().success());
}

#[cfg(windows)]
#[test]
fn native_console_ctrl_c_and_break_restore_modes() {
    use std::os::windows::process::CommandExt;
    let temp = TempDir::new().unwrap();
    let probe = temp.path().join("console-probe.exe");
    assert!(Command::new("rustc")
        .args([
            "--edition=2021",
            concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/tests/support/console_probe.rs"
            ),
            "-o"
        ])
        .arg(&probe)
        .status()
        .unwrap()
        .success());
    for command in ["board", "tail"] {
        for control in ["0", "1"] {
            let home = temp.path().join(format!("{command}-{control}"));
            fs::create_dir_all(&home).unwrap();
            let output = Command::new(&probe)
                .env_clear()
                .env("SystemRoot", std::env::var_os("SystemRoot").unwrap())
                .env("TEMP", &home)
                .env("TMP", &home)
                .env("LOCALAPPDATA", home.join("AppData/Local"))
                .env("APPDATA", home.join("AppData/Roaming"))
                .args([BIN, command, control])
                .creation_flags(0x10)
                .env("HOME", &home)
                .env("USERPROFILE", &home)
                .env("AGREP_HOME", &home)
                .env("AGREP_DATA_DIR", home.join("data"))
                .env_remove("AGREP_MODEL_DIR")
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{command} control {control}: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
}
