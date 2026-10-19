mod common;

use common::*;
use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const CHILD_SCHEDULING_WATCHDOG: Duration = Duration::from_secs(15);
const WARM_EXIT_BUDGET: Duration = Duration::from_secs(2);
const CHILD_TEARDOWN_BUDGET: Duration = Duration::from_secs(2);

fn scrub(command: &mut Command, home: &std::path::Path, data: &std::path::Path) {
    command.env("AGREP_HOME", home).env("AGREP_DATA_DIR", data);
    for name in [
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "CLINE_DIR",
        "XDG_CONFIG_HOME",
        "AGREP_RS_BIN",
        "AGREP_DEBUG",
    ] {
        command.env_remove(name);
    }
}

fn current_corpusdb_label(data: &Path) -> String {
    let owner: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap()).unwrap();
    format!("corpusdb:{}", owner["build_id"].as_str().unwrap())
}

fn start_holder(home: &std::path::Path, data: &std::path::Path, label: &str) -> Child {
    let mut command = Command::new(BIN);
    command
        .args([
            "index-lock-contract",
            "hold",
            "--path",
            data.join(".index.lock").to_str().unwrap(),
            "--label",
            label,
            "--timeout-ms",
            "1000",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    scrub(&mut command, home, data);
    let mut child = command.spawn().unwrap();
    let mut ready = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    assert!(ready.contains(r#""ready":true"#), "{ready}");
    child
}

fn start_index_for(home: &std::path::Path, data: &std::path::Path, agent: &str) -> Child {
    let mut command = Command::new(BIN);
    command
        .args(["index", "--agent", agent])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    scrub(&mut command, home, data);
    command.spawn().unwrap()
}

fn start_index(home: &std::path::Path, data: &std::path::Path) -> Child {
    start_index_for(home, data, "claude")
}

fn release_holder(holder: &mut Child) {
    drop(holder.stdin.take());
    assert!(holder.wait().unwrap().success());
}

fn wait_for_status(child: &mut Child, timeout: Duration) -> Option<ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) => {}
            Err(_) => return None,
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn wait_for_exit(child: &mut Child, timeout: Duration) -> bool {
    wait_for_status(child, timeout).is_some()
}

fn terminate_and_reap(child: &mut Child) -> bool {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return true;
    }
    let _ = child.kill();
    wait_for_exit(child, CHILD_TEARDOWN_BUDGET)
}

fn await_child_scheduling(home: &Path, data: &Path) {
    let mut command = Command::new(BIN);
    command
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    scrub(&mut command, home, data);
    let mut child = command.spawn().unwrap();
    let Some(status) = wait_for_status(&mut child, CHILD_SCHEDULING_WATCHDOG) else {
        let reaped = terminate_and_reap(&mut child);
        panic!("test child was not scheduled before the watchdog; reaped={reaped}");
    };
    assert!(status.success(), "scheduling probe failed: {status}");
}

fn durable_tree(root: &Path) -> BTreeMap<String, Vec<u8>> {
    fn fingerprint(metadata: &fs::Metadata) -> Vec<u8> {
        let modified_ns = metadata
            .modified()
            .ok()
            .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|value| value.as_nanos())
            .unwrap_or(u128::MAX);
        let mut state = format!(
            "len={} modified_ns={modified_ns} readonly={}",
            metadata.len(),
            metadata.permissions().readonly()
        )
        .into_bytes();
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            state.extend_from_slice(
                format!(
                    " dev={} ino={} mode={} mtime={}.{} ctime={}.{}",
                    metadata.dev(),
                    metadata.ino(),
                    metadata.mode(),
                    metadata.mtime(),
                    metadata.mtime_nsec(),
                    metadata.ctime(),
                    metadata.ctime_nsec()
                )
                .as_bytes(),
            );
        }
        state
    }

    fn walk(root: &Path, path: &Path, out: &mut BTreeMap<String, Vec<u8>>) {
        let mut entries: Vec<_> = fs::read_dir(path)
            .unwrap()
            .map(|entry| entry.unwrap())
            .collect();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let name = entry.file_name();
            if path == root
                && matches!(
                    name.to_str(),
                    Some(".index.lock" | ".indexd.lock" | ".indexd.v2.lock")
                )
            {
                continue;
            }
            let entry_path = entry.path();
            let relative = entry_path.strip_prefix(root).unwrap().to_string_lossy();
            let metadata = fs::symlink_metadata(&entry_path).unwrap();
            if metadata.is_dir() {
                out.insert(format!("d:{relative}"), fingerprint(&metadata));
                walk(root, &entry_path, out);
            } else if metadata.is_file() {
                let mut state = fingerprint(&metadata);
                state.push(0);
                state.extend_from_slice(&fs::read(&entry_path).unwrap());
                out.insert(format!("f:{relative}"), state);
            } else {
                out.insert(format!("s:{relative}"), fingerprint(&metadata));
            }
        }
    }

    let mut out = BTreeMap::new();
    walk(root, root, &mut out);
    out
}

fn assert_no_temp_residue(root: &Path) {
    for name in durable_tree(root).keys() {
        assert!(
            !name.contains(".tmp.") && !name.ends_with(".tmp"),
            "temporary artifact survived: {name}"
        );
    }
    for name in [".indexd.lock", ".indexd.v2.lock", ".ingest_pending.bin"] {
        assert!(
            !root.join(name).exists(),
            "ephemeral artifact survived: {name}"
        );
    }
}

fn assert_artifact_blocks_bypass(name: &str, body: &[u8]) {
    let home = fixture_home("claude");
    let data = temp_dir(&format!("warm-lock-barrier-{}", name.replace('.', "-")));
    ingest_into("claude", &home, &data, false);
    fs::write(data.join(name), body).unwrap();
    let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
    let mut index = start_index(&home, &data);
    thread::sleep(Duration::from_millis(250));
    assert!(index.try_wait().unwrap().is_none(), "{name} was bypassed");
    fs::remove_file(data.join(name)).unwrap();
    release_holder(&mut holder);
    let output = index.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_no_temp_residue(&data);
    let _ = fs::remove_dir_all(data);
}

#[test]
fn unchanged_generation_does_not_queue_behind_corpusdb() {
    let home = fixture_home("claude");
    let data = temp_dir("warm-lock-bypass");
    ingest_into("claude", &home, &data, false);
    let before = durable_tree(&data);
    assert!(before
        .keys()
        .any(|name| { name.replace('\\', "/") == "f:events/.store.sqlite3-shm" }));
    let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
    let mut index = start_index(&home, &data);
    if !wait_for_exit(&mut index, Duration::from_secs(2)) {
        release_holder(&mut holder);
        let _ = index.wait();
        panic!("unchanged index waited for the corpusdb lock");
    }
    assert!(holder.try_wait().unwrap().is_none());
    let output = index.wait_with_output().unwrap();
    release_holder(&mut holder);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    let lines: Vec<_> = stdout.lines().collect();
    assert_eq!(lines.len(), 2, "{stdout}");
    assert!(
        lines[0].starts_with("  unchanged since last index (")
            && lines[0].contains(" messages); skipped ingest + writes (")
            && lines[0].ends_with("ms)"),
        "{stdout}"
    );
    let full_ms = lines[0]
        .rsplit_once('(')
        .and_then(|(_, value)| value.strip_suffix("ms)"))
        .unwrap();
    assert!(
        full_ms.bytes().all(|byte| byte.is_ascii_digit()),
        "{stdout}"
    );
    let source_ms = lines[1]
        .strip_prefix("  phases: source-check ")
        .and_then(|value| value.strip_suffix("ms"))
        .unwrap();
    assert!(
        source_ms.bytes().all(|byte| byte.is_ascii_digit()),
        "{stdout}"
    );
    assert!(
        output.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(durable_tree(&data), before);
    assert_no_temp_residue(&data);
    let _ = fs::remove_dir_all(data);
}

#[test]
fn unchanged_default_all_generation_bypasses_zero_row_agent_proofs() {
    let home = fixture_home("claude");
    let data = temp_dir("warm-lock-bypass-all");
    ingest_into("all", &home, &data, false);
    let before = durable_tree(&data);
    await_child_scheduling(&home, &data);
    let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
    let mut index = start_index_for(&home, &data, "all");
    if !wait_for_exit(&mut index, WARM_EXIT_BUDGET) {
        let index_reaped = terminate_and_reap(&mut index);
        let holder_reaped = terminate_and_reap(&mut holder);
        panic!(
            "unchanged default index exceeded the 2s warm-exit budget; cleanup index={index_reaped} holder={holder_reaped}"
        );
    }
    assert!(holder.try_wait().unwrap().is_none());
    let output = index.wait_with_output().unwrap();
    release_holder(&mut holder);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stderr.is_empty());
    let stdout = String::from_utf8(output.stdout).unwrap();
    let lines: Vec<_> = stdout.lines().collect();
    assert_eq!(lines.len(), 2, "{stdout}");
    assert!(
        lines[0].contains("messages); skipped ingest + writes ("),
        "{stdout}"
    );
    assert!(lines[1].starts_with("  phases: source-check "), "{stdout}");
    assert_eq!(durable_tree(&data), before);
    let _ = fs::remove_dir_all(data);
}

#[test]
fn changed_source_waits_for_the_writer_lock() {
    let home = temp_dir("warm-lock-changed-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("warm-lock-changed-data");
    ingest_into("claude", &home, &data, false);
    let source = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    let body = fs::read_to_string(&source)
        .unwrap()
        .replace("flaky timer test", "changed timer test");
    fs::write(&source, body).unwrap();
    let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
    let mut index = start_index(&home, &data);
    thread::sleep(Duration::from_millis(250));
    assert!(index.try_wait().unwrap().is_none());
    release_holder(&mut holder);
    let output = index.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(normalize(&data).contains("changed timer test"));
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

#[test]
fn cleanup_artifacts_disable_the_unlocked_skip() {
    for name in [
        ".ingest_pending.bin",
        ".source_absence_pending",
        ".source-health.json",
    ] {
        assert_artifact_blocks_bypass(name, b"barrier");
    }
}

#[test]
fn incomplete_event_authority_disables_the_unlocked_skip() {
    let home = fixture_home("claude");
    let data = temp_dir("warm-lock-event-authority");
    ingest_into("claude", &home, &data, false);
    let proof_path = data.join(".events_complete.claude.json");
    let proof = fs::read(&proof_path).unwrap();
    fs::remove_file(&proof_path).unwrap();
    let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
    let mut index = start_index(&home, &data);
    thread::sleep(Duration::from_millis(250));
    assert!(index.try_wait().unwrap().is_none());
    fs::write(&proof_path, proof).unwrap();
    release_holder(&mut holder);
    let output = index.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_no_temp_residue(&data);
    let _ = fs::remove_dir_all(data);
}

#[test]
fn stale_event_proof_preflight_is_read_only() {
    for stale_state in [false, true] {
        let home = fixture_home("claude");
        let data = temp_dir(if stale_state {
            "warm-lock-event-state"
        } else {
            "warm-lock-event-inventory"
        });
        ingest_into("claude", &home, &data, false);
        let proof_path = data.join(".events_complete.claude.json");
        let original = fs::read(&proof_path).unwrap();
        let mut proof: serde_json::Value = serde_json::from_slice(&original).unwrap();
        if stale_state {
            let byte = proof["generation_value"][0].as_u64().unwrap();
            proof["generation_value"][0] = serde_json::Value::from(byte ^ 1);
        } else {
            let hash = proof["inventory_hash"].as_u64().unwrap();
            proof["inventory_hash"] = serde_json::Value::from(hash ^ 1);
        }
        fs::write(&proof_path, serde_json::to_vec(&proof).unwrap()).unwrap();
        let before = durable_tree(&data);
        let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
        let mut index = start_index(&home, &data);
        thread::sleep(Duration::from_millis(1200));
        assert!(index.try_wait().unwrap().is_none());
        assert_eq!(durable_tree(&data), before);
        fs::write(&proof_path, original).unwrap();
        release_holder(&mut holder);
        let output = index.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_no_temp_residue(&data);
        let _ = fs::remove_dir_all(data);
    }
}

#[test]
fn missing_event_shm_preflight_does_not_open_sqlite() {
    let home = fixture_home("claude");
    let data = temp_dir("warm-lock-event-shm");
    ingest_into("claude", &home, &data, false);
    let shm = data.join("events").join(".store.sqlite3-shm");
    let saved = data.with_extension("saved-event-shm");
    fs::rename(&shm, &saved).unwrap();
    let before = durable_tree(&data);
    let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
    let mut index = start_index(&home, &data);
    thread::sleep(Duration::from_millis(1200));
    assert!(index.try_wait().unwrap().is_none());
    assert_eq!(durable_tree(&data), before);
    assert!(!shm.exists(), "read-only preflight recreated SQLite SHM");
    fs::rename(&saved, &shm).unwrap();
    release_holder(&mut holder);
    let output = index.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_no_temp_residue(&data);
    let _ = fs::remove_dir_all(data);
}

#[test]
fn nonzero_event_wal_preflight_does_not_open_sqlite() {
    let home = fixture_home("claude");
    let data = temp_dir("warm-lock-event-wal");
    ingest_into("claude", &home, &data, false);
    let wal = data.join("events").join(".store.sqlite3-wal");
    let saved = data.with_extension("saved-event-wal");
    fs::rename(&wal, &saved).unwrap();
    fs::write(&wal, b"uncommitted").unwrap();
    let before = durable_tree(&data);
    let mut holder = start_holder(&home, &data, &current_corpusdb_label(&data));
    let mut index = start_index(&home, &data);
    thread::sleep(Duration::from_millis(1200));
    assert!(index.try_wait().unwrap().is_none());
    assert_eq!(durable_tree(&data), before);
    fs::remove_file(&wal).unwrap();
    fs::rename(&saved, &wal).unwrap();
    release_holder(&mut holder);
    let output = index.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_no_temp_residue(&data);
    let _ = fs::remove_dir_all(data);
}

#[test]
fn plain_and_foreign_corpusdb_holders_cannot_bypass() {
    for foreign_holder in [false, true] {
        let home = fixture_home("claude");
        let data = temp_dir(if foreign_holder {
            "warm-lock-untrusted-foreign"
        } else {
            "warm-lock-untrusted-plain"
        });
        ingest_into("claude", &home, &data, false);
        let current = current_corpusdb_label(&data);
        let label = if foreign_holder {
            if current == "corpusdb:aaaaaaaaaaaaaaaaaaaa" {
                "corpusdb:bbbbbbbbbbbbbbbbbbbb".to_string()
            } else {
                "corpusdb:aaaaaaaaaaaaaaaaaaaa".to_string()
            }
        } else {
            "corpusdb".to_string()
        };
        let mut holder = start_holder(&home, &data, &label);
        let mut index = start_index(&home, &data);
        thread::sleep(Duration::from_millis(250));
        assert!(index.try_wait().unwrap().is_none(), "bypassed {label}");
        release_holder(&mut holder);
        let output = index.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let _ = fs::remove_dir_all(data);
    }
}

/// Backdate `path` past the publication grace a freshly written owner record gets.
fn age_past_publication_grace(path: &Path) {
    let aged = std::time::SystemTime::now() - Duration::from_secs(10);
    fs::File::options()
        .write(true)
        .open(path)
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(aged))
        .unwrap();
}

/// Index a churned claude chat in `home` beside a freshness-daemon owner record `body` at `name`,
/// backdated past its publication grace. Returns the pass and whether it published the churn.
fn index_beside_a_stale_daemon_record(name: &str, body: &[u8]) -> (std::process::Output, bool) {
    let home = temp_dir("stale-daemon-record-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("stale-daemon-record-data");
    ingest_into("all", &home, &data, false);
    let record = data.join(name);
    fs::write(&record, body).unwrap();
    age_past_publication_grace(&record);
    let chat = home.join(".claude/projects/proj-beta/22222222-2222-4222-8222-222222222222.jsonl");
    fs::create_dir_all(chat.parent().unwrap()).unwrap();
    let row = serde_json::json!({
        "type": "user", "userType": "external",
        "sessionId": "22222222-2222-4222-8222-222222222222",
        "timestamp": "2026-01-03T10:01:00.000Z", "cwd": "/work/beta",
        "message": {"role": "user", "content": "churn past a stale daemon record"},
    });
    fs::write(&chat, format!("{row}\n")).unwrap();
    let output = ingest_output("all", &home, &data, false);
    let published = normalize(&data).contains("churn past a stale daemon record");
    if published {
        assert!(
            !record.exists(),
            "{name} survived the pass that reclaimed it"
        );
        assert_no_temp_residue(&data);
    } else {
        assert_eq!(
            fs::read(&record).unwrap(),
            body,
            "{name} changed under a live owner"
        );
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
    (output, published)
}

/// A writer killed between creating its derived-adoption claim and writing it leaves the record
/// empty or torn. Past the publication grace no live owner can stand behind it: the next pass
/// reclaims it and publishes, rather than serving the snapshot read-only for good.
#[test]
fn a_stale_empty_or_torn_daemon_record_no_longer_holds_the_index_read_only() {
    let torn = format!("state=derived-adoption pid={}", u32::MAX >> 1);
    for (name, body) in [
        (".indexd.v2.lock", &b""[..]),
        (".indexd.lock", &b""[..]),
        (".indexd.v2.lock", torn.as_bytes()),
        (".indexd.lock", &b"state=derived-adoption pi"[..]),
    ] {
        let (output, published) = index_beside_a_stale_daemon_record(name, body);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(output.status.success(), "{name}: {stderr}");
        assert!(!stderr.contains("read-only"), "{name}: {stderr}");
        assert!(
            published,
            "{name} {body:?}: the churn was not published\n{stderr}"
        );
    }
}

/// The control: a record naming a live process as a complete owner or a derived-adoption claim
/// is a real foreign lock at any age. The pass still declines to write and says why.
#[test]
fn a_daemon_record_naming_a_live_owner_keeps_the_index_read_only_at_any_age() {
    let pid = std::process::id();
    let (writer, token) = ("e".repeat(20), "f".repeat(32));
    for (name, body) in [
        (
            ".indexd.v2.lock",
            format!(
                "pid={pid} start=fixture protocol=2 package=x build=y writer={writer} \
                 group=1 token={token} time=1\n"
            ),
        ),
        (
            ".indexd.v2.lock",
            format!(
                "state=derived-adoption pid={pid} start=unknown writer={writer} token={token}\n"
            ),
        ),
        (
            ".indexd.lock",
            format!("pid={pid} start=fixture protocol=1 package=0.2.0 build=legacy\n"),
        ),
    ] {
        let (output, published) = index_beside_a_stale_daemon_record(name, body.as_bytes());
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(output.status.success(), "{name}: {stderr}");
        assert!(!published, "{name}: a live owner's record was reclaimed");
        assert!(
            stderr.contains("freshness-daemon ownership")
                || stderr.contains("freshness-daemon owner"),
            "{name}: {stderr}"
        );
        assert!(
            stderr.contains("serving the published snapshot read-only"),
            "{stderr}"
        );
    }
}
