#![cfg(any(target_os = "linux", target_os = "macos"))]

use std::fs;
use std::os::fd::AsRawFd;
use std::path::PathBuf;

use agrep_core::ingest::registry::nofollow_read_options;

fn temp_dir(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "agrep-nofollow-{tag}-{}-{nanos}",
        std::process::id()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn hardened_open_refuses_a_final_symlink_and_never_blocks() {
    let dir = temp_dir("open");
    let target = dir.join("target.jsonl");
    fs::write(&target, b"{}\n").unwrap();
    let link = dir.join("link.jsonl");
    std::os::unix::fs::symlink(&target, &link).unwrap();

    let mut options = fs::OpenOptions::new();
    options.read(true);
    nofollow_read_options(&mut options);

    let error = options
        .open(&link)
        .expect_err("a hardened open followed a final-component symlink");
    assert_eq!(error.raw_os_error(), Some(libc::ELOOP), "{error}");

    let file = options.open(&target).unwrap();
    let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
    assert!(flags >= 0, "{}", std::io::Error::last_os_error());
    assert_ne!(
        flags & libc::O_NONBLOCK,
        0,
        "hardened open is blocking: {flags:#x}"
    );

    fs::remove_dir_all(&dir).unwrap();
}
