//! One test in its own binary. Tests that share a process fork children from parallel
//! threads, and a child between fork and exec holds every descriptor of the process, this
//! test's pipe included, so the pipe check below is only exact without neighbours.
#![cfg(unix)]
#[path = "../examples/support/exit.rs"]
mod exit;
mod support;
use agrep_core::data::DataRoot;
use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};
use tempfile::TempDir;

fn write_chat(path: &Path, id: &str, cwd: &Path, turns: &[(&str, &str, &str)]) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut lines = vec![
        json!({"type":"session","id":id,"version":3,"cwd":cwd,"timestamp":"2026-01-02T00:00:00Z"})
            .to_string(),
    ];
    let mut parent = Value::Null;
    for (i, (ts, text, reply)) in turns.iter().enumerate() {
        let uid = format!("u{i}");
        lines.push(json!({"type":"message","id":uid,"parentId":parent,"timestamp":ts,"message":{"role":"user","content":[{"type":"text","text":text}],"timestamp":ts}}).to_string());
        parent = json!(uid);
        if !reply.is_empty() {
            let aid = format!("a{i}");
            lines.push(json!({"type":"message","id":aid,"parentId":parent,"timestamp":ts,"message":{"role":"assistant","model":"test","content":[{"type":"text","text":reply}],"timestamp":ts}}).to_string());
            parent = json!(aid);
        }
    }
    fs::write(path, lines.join("\n") + "\n").unwrap();
}

fn fixture() -> TempDir {
    let t = TempDir::new().unwrap();
    let cwd = t.path().join("project alpha");
    fs::create_dir_all(&cwd).unwrap();
    write_chat(
        &t.path()
            .join("home/.pi/agent/sessions/alpha/2026-01-02_root-alpha-0001.jsonl"),
        "root-alpha-0001",
        &cwd,
        &[("2026-01-02T00:00:00Z", "design the cache", "Finished.")],
    );
    let out = Command::new(support::binary())
        .env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .args(["index"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    t
}

#[test]
fn a_spawned_worker_holds_no_pipe_of_the_caller() {
    use std::io::Read;
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    use std::time::Duration;
    let t = fixture();
    let root = DataRoot(t.path().join("data"));
    // Leave several FTS segments for the search's merger, then keep it queued.
    let source = t
        .path()
        .join("home/.pi/agent/sessions/alpha/2026-01-02_root-alpha-0001.jsonl");
    for n in 2..=3 {
        let turns = vec![("2026-01-02T00:00:00Z", "design the cache", "Finished."); n];
        write_chat(
            &source,
            "root-alpha-0001",
            &t.path().join("project alpha"),
            &turns,
        );
        let out = Command::new(support::binary())
            .env_clear()
            .env("HOME", t.path().join("home"))
            .env("AGREP_HOME", t.path().join("home"))
            .env("AGREP_DATA_DIR", t.path().join("data"))
            .args(["__work", "refresh"])
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    assert!(agrep_core::fts_segments::pending(&agrep_core::store::reader(&root).unwrap()).unwrap());
    let queued = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(root.path().join("writer.queue"))
        .unwrap();
    queued.lock_shared().unwrap();
    let (mut reader, writer) = std::io::pipe().unwrap();
    let extra = writer.as_raw_fd();
    let mut cmd = Command::new(support::binary());
    cmd.env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_QUERY_TIMING", "1")
        .args(["search", "cache"]);
    // `out=$(agrep search cache 3>&1)`: the caller reads its pipe to the end.
    unsafe {
        cmd.pre_exec(move || {
            let done = if extra == 3 {
                libc::fcntl(3, libc::F_SETFD, 0)
            } else {
                libc::dup2(extra, 3)
            };
            if done < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let out = cmd.output().unwrap();
    drop(writer);
    let worker = String::from_utf8_lossy(&out.stderr)
        .lines()
        .find_map(|l| serde_json::from_str::<Value>(l).ok()?["worker_started"]["pid"].as_u64())
        .expect("the search started no worker") as u32;
    let running = !exit::wait(worker, Duration::ZERO).unwrap();
    let flags = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_GETFL) };
    assert_eq!(
        unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) },
        0
    );
    // The search has exited, so only the worker can still hold the pipe open.
    let held =
        matches!(reader.read(&mut [0; 1]), Err(e) if e.kind() == std::io::ErrorKind::WouldBlock);
    drop(queued);
    assert!(exit::wait(worker, Duration::from_secs(30)).unwrap());
    assert!(running, "the worker exited before the pipe was checked");
    assert!(!held, "the worker inherited the caller's pipe");
}
