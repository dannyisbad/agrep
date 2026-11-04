//! Standalone Windows console fixture, compiled into a disposable directory by live.rs.
use std::{
    fs::OpenOptions,
    io::{BufRead, BufReader},
    os::windows::{io::AsRawHandle, process::CommandExt},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
#[link(name = "kernel32")]
extern "system" {
    fn GetConsoleMode(handle: *mut std::ffi::c_void, mode: *mut u32) -> i32;
    fn SetConsoleCtrlHandler(
        handler: Option<unsafe extern "system" fn(u32) -> i32>,
        add: i32,
    ) -> i32;
    fn GenerateConsoleCtrlEvent(event: u32, group: u32) -> i32;
    fn WaitForSingleObject(handle: *mut std::ffi::c_void, millis: u32) -> u32;
}
fn main() {
    let args: Vec<_> = std::env::args().collect();
    let input = OpenOptions::new()
        .read(true)
        .write(true)
        .open("CONIN$")
        .unwrap();
    let output = OpenOptions::new()
        .read(true)
        .write(true)
        .open("CONOUT$")
        .unwrap();
    let mut before = 0;
    assert_ne!(
        unsafe { GetConsoleMode(input.as_raw_handle(), &mut before) },
        0
    );
    let mut output_before = 0;
    assert_ne!(
        unsafe { GetConsoleMode(output.as_raw_handle(), &mut output_before) },
        0
    );
    let output_check = output.try_clone().unwrap();
    let command = &args[2];
    let control: u32 = args[3].parse().unwrap();
    let mut cmd = Command::new(&args[1]);
    cmd.arg(command)
        .stdin(input.try_clone().unwrap())
        .stderr(Stdio::null());
    if control == 1 {
        cmd.creation_flags(0x200);
    }
    if command == "board" {
        cmd.stdout(output);
    } else {
        cmd.stdout(Stdio::piped());
    }
    if command == "serve" || command == "ui" {
        cmd.args(["--port", "0"]);
    }
    // The foreground command must opt back in even when its parent ignores
    // Ctrl-C (this attribute is inherited, unlike handler registrations).
    assert_ne!(unsafe { SetConsoleCtrlHandler(None, 1) }, 0);
    let mut child = cmd.spawn().unwrap();
    let ready = if command == "board" {
        let deadline = Instant::now() + Duration::from_secs(10);
        // Console modes change without notification; poll them, waking early if the command exits.
        loop {
            let mut mode = 0;
            assert_ne!(
                unsafe { GetConsoleMode(input.as_raw_handle(), &mut mode) },
                0
            );
            // Wait for fully configured signal-preserving raw mode, not the
            // transient mode set inside crossterm::enable_raw_mode.
            if mode & 1 != 0 && mode & 6 == 0 {
                break true;
            }
            if Instant::now() > deadline {
                break false;
            }
            assert_eq!(
                unsafe { WaitForSingleObject(child.as_raw_handle(), 10) },
                0x102,
                "command exited before ready"
            );
        }
    } else {
        let (tx, rx) = std::sync::mpsc::channel();
        let stdout = child.stdout.take().unwrap();
        std::thread::spawn(move || {
            let mut line = String::new();
            let _ = BufReader::new(stdout).read_line(&mut line);
            let _ = tx.send(!line.is_empty());
        });
        rx.recv_timeout(Duration::from_secs(10)).unwrap_or(false)
    };
    if !ready {
        let _ = child.kill();
        let _ = child.wait();
        panic!("command did not become ready");
    }
    assert_ne!(
        unsafe { GenerateConsoleCtrlEvent(control, if control == 0 { 0 } else { child.id() }) },
        0
    );
    if unsafe { WaitForSingleObject(child.as_raw_handle(), 10_000) } != 0 {
        let _ = child.kill();
        let _ = child.wait();
        panic!("console control event did not exit command");
    }
    let status = child.wait().unwrap();
    assert_eq!(status.code(), Some(if control == 0 { 130 } else { 3 }));
    let mut after = 0;
    assert_ne!(
        unsafe { GetConsoleMode(input.as_raw_handle(), &mut after) },
        0
    );
    assert_eq!(before, after, "console input mode was not restored");
    let mut output_after = 0;
    assert_ne!(
        unsafe { GetConsoleMode(output_check.as_raw_handle(), &mut output_after) },
        0
    );
    assert_eq!(
        output_before, output_after,
        "console output mode was not restored"
    );
}
