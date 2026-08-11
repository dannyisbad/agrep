//! Torture suite: hostile store files must never panic the ingest, never break the
//! intake accounting identity, and never poison neighbouring good files. Each JSONL/JSON
//! adapter gets the same five byte-level mutations planted next to its pristine fixture
//! (truncated tail, binary garbage, invalid UTF-8, empty file, BOM prefix); the claude
//! and codex parsers additionally get a byte-offset truncation sweep proving rows are
//! only ever lost at the cut, never invented. Sqlite stores are exercised under a live
//! WAL writer and an exclusive lock (antigravity's brain-dir layout is too structured
//! for generic byte planting; its read/parse error arms share read_lossy and the
//! identity convention, covered by the shared code paths above).

mod common;

use common::*;
use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier};

/// (session, turn, text) projection of messages.jsonl - the content identity a hostile
/// parse may LOSE rows from but must never invent rows into (row JSON itself can shift
/// legitimately, e.g. a reply cut off mid-attachment).
fn msg_keys(data: &Path) -> HashSet<(String, u64, String)> {
    sorted_lines(&data.join("messages.jsonl"))
        .iter()
        .map(|l| {
            let v: serde_json::Value = serde_json::from_str(l).expect("messages.jsonl row parses");
            (
                v["session"].as_str().unwrap_or("").to_string(),
                v["turn"].as_u64().unwrap_or(0),
                v["text"].as_str().unwrap_or("").to_string(),
            )
        })
        .collect()
}

/// Same-length byte substitution, used to re-key a copied fixture's session id so
/// torture rows land in their own sessions instead of colliding with the pristine ones.
fn replace_bytes(hay: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    assert_eq!(from.len(), to.len(), "id replacement must preserve length");
    let mut out = hay.to_vec();
    let mut i = 0;
    while i + from.len() <= out.len() {
        if &out[i..i + from.len()] == from {
            out[i..i + from.len()].copy_from_slice(to);
            i += from.len();
        } else {
            i += 1;
        }
    }
    out
}

/// The five mutations every store format must survive. Derived from the adapter's own
/// fixture bytes so each variant is hostile in exactly one way.
fn mutations(seed: &[u8]) -> Vec<(&'static str, Vec<u8>)> {
    let cut = seed.len() * 618 / 1000;

    let nl = seed
        .iter()
        .position(|b| *b == b'\n')
        .map(|i| i + 1)
        .unwrap_or(0);
    let mut garbage = Vec::new();
    garbage.extend_from_slice(&seed[..nl]);
    garbage.extend_from_slice(b"\x00\x01\x02 not json at all {{{\n");
    garbage.extend_from_slice(&seed[nl..]);
    garbage.extend_from_slice(b"trailing junk with no terminating newline");

    let mut bad = seed.to_vec();
    let mid = (bad.len() * 2 / 5).min(bad.len().saturating_sub(2));
    let pos = (mid..bad.len() - 1)
        .find(|&i| bad[i] != b'\n' && bad[i + 1] != b'\n')
        .unwrap_or(mid);
    bad[pos] = 0xFF;
    bad[pos + 1] = 0xFE;

    let mut bom = vec![0xEF, 0xBB, 0xBF];
    bom.extend_from_slice(seed);

    vec![
        ("truncated", seed[..cut].to_vec()),
        ("garbage", garbage),
        ("badutf8", bad),
        ("empty", Vec::new()),
        ("bom", bom),
    ]
}

struct Target {
    adapter: &'static str,
    /// fixture-home-relative path of the file the mutations are derived from
    seed: &'static [&'static str],
    /// session-id bytes inside the seed to re-key per variant (empty = identity is
    /// the file/dir name, no in-content re-keying needed)
    id: &'static str,
    /// place variant `idx` of `bytes` at a discoverable path inside `home`
    plant: fn(home: &Path, idx: usize, bytes: &[u8]),
}

fn seed_path(home: &Path, rel: &[&str]) -> PathBuf {
    let mut p = home.to_path_buf();
    for c in rel {
        p = p.join(c);
    }
    p
}

/// Fresh 36-char uuid-shaped id for torture variant `idx`, disjoint from every fixture id.
fn torture_uuid(idx: usize) -> String {
    format!("9999999{idx}-9999-4999-8999-999999999999")
}

const TARGETS: &[Target] = &[
    Target {
        adapter: "claude",
        seed: &[
            ".claude",
            "projects",
            "proj-alpha",
            "sess-claude-0001.jsonl",
        ],
        id: "11111111-1111-4111-8111-111111111111",
        plant: |home, idx, bytes| {
            let p = home
                .join(".claude")
                .join("projects")
                .join("proj-alpha")
                .join(format!("torture-{idx}.jsonl"));
            fs::write(p, bytes).unwrap();
        },
    },
    Target {
        adapter: "codex",
        seed: &[
            ".codex",
            "sessions",
            "2026",
            "01",
            "02",
            "rollout-2026-01-02T10-00-00-22222222-2222-4222-8222-222222222222.jsonl",
        ],
        id: "22222222-2222-4222-8222-222222222222",
        plant: |home, idx, bytes| {
            let dir = home
                .join(".codex")
                .join("sessions")
                .join("2026")
                .join("01")
                .join("03");
            fs::create_dir_all(&dir).unwrap();
            fs::write(
                dir.join(format!(
                    "rollout-2026-01-03T09-00-0{idx}-{}.jsonl",
                    torture_uuid(idx)
                )),
                bytes,
            )
            .unwrap();
        },
    },
    Target {
        adapter: "kimi",
        seed: &[
            ".kimi",
            "sessions",
            "fab17762fe0b032c1fe6f3196408f257",
            "44444444-4444-4444-8444-444444444444",
            "context.jsonl",
        ],
        id: "",
        plant: |home, idx, bytes| {
            let dir = home
                .join(".kimi")
                .join("sessions")
                .join("fab17762fe0b032c1fe6f3196408f257")
                .join(torture_uuid(idx));
            fs::create_dir_all(&dir).unwrap();
            fs::write(dir.join("context.jsonl"), bytes).unwrap();
        },
    },
    Target {
        adapter: "gemini",
        seed: &[
            ".gemini",
            "tmp",
            "hash5555synthetic",
            "chats",
            "session-2026-01-02T10-00-55555555.json",
        ],
        id: "55555555-5555-4555-8555-555555555555",
        plant: |home, idx, bytes| {
            let p = home
                .join(".gemini")
                .join("tmp")
                .join("hash5555synthetic")
                .join("chats")
                .join(format!("session-2026-01-03T09-00-torture{idx}.json"));
            fs::write(p, bytes).unwrap();
        },
    },
    Target {
        adapter: "cline",
        seed: &[
            ".cline",
            "data",
            "tasks",
            "1767348000000",
            "api_conversation_history.json",
        ],
        id: "",
        plant: |home, idx, bytes| {
            let dir = home
                .join(".cline")
                .join("data")
                .join("tasks")
                .join(format!("176734810000{idx}"));
            fs::create_dir_all(&dir).unwrap();
            fs::write(dir.join("api_conversation_history.json"), bytes).unwrap();
        },
    },
];

/// The core torture contract, per adapter: plant all five hostile variants beside the
/// pristine fixture, ingest, and require (1) a clean exit, (2) the intake identity on
/// every tallied file, (3) every pristine message row still present - hostile input may
/// cost its own records, never a neighbour's.
fn torture_adapter(t: &Target) {
    let seed = fs::read(seed_path(&fixture_home(t.adapter), t.seed)).unwrap();

    let baseline_data = temp_dir(&format!("torture-base-{}", t.adapter));
    ingest_into(t.adapter, &fixture_home(t.adapter), &baseline_data, true);
    let baseline: HashSet<String> = sorted_lines(&baseline_data.join("messages.jsonl"))
        .into_iter()
        .collect();
    assert!(
        !baseline.is_empty(),
        "{}: baseline fixture produced no rows",
        t.adapter
    );

    let home = temp_dir(&format!("torture-home-{}", t.adapter));
    copy_dir(&fixture_home(t.adapter), &home);
    for (idx, (_name, bytes)) in mutations(&seed).into_iter().enumerate() {
        let rekeyed = if t.id.is_empty() {
            bytes
        } else {
            replace_bytes(&bytes, t.id.as_bytes(), torture_uuid(idx).as_bytes())
        };
        (t.plant)(&home, idx, &rekeyed);
    }

    let data = temp_dir(&format!("torture-{}", t.adapter));
    ingest_into(t.adapter, &home, &data, true);
    check_intake_identity(&data);

    // vacuity guard: discovery must have actually reached the planted files - a moved
    // plant path would otherwise reduce this test to pristine == pristine, green forever
    let book: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(data.join("intake_stats.json")).unwrap()).unwrap();
    let torture_entries: Vec<(&String, &serde_json::Value)> = book["files"]
        .as_object()
        .unwrap()
        .iter()
        .filter(|(k, _)| k.contains("torture") || k.contains("9999999"))
        .collect();
    assert!(
        torture_entries.len() >= 4,
        "{}: only {} planted torture files reached the parser (expected >=4; discovery \
         no longer sees the plant location?)",
        t.adapter,
        torture_entries.len()
    );
    let torture_seen: u64 = torture_entries
        .iter()
        .map(|(_, e)| e["seen"].as_u64().unwrap_or(0))
        .sum();
    assert!(
        torture_seen > 0,
        "{}: planted torture files were discovered but nothing in them was ever iterated",
        t.adapter
    );

    let tortured: HashSet<String> = sorted_lines(&data.join("messages.jsonl"))
        .into_iter()
        .collect();
    let missing: Vec<&String> = baseline.difference(&tortured).collect();
    assert!(
        missing.is_empty(),
        "{}: torture files poisoned pristine rows; missing after torture:\n{:#?}",
        t.adapter,
        missing
    );

    let _ = fs::remove_dir_all(&baseline_data);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A slow host legitimately defers a hot tail into pending; convergence gets a
/// bounded window of quiescent passes instead of one instantaneous assert.
fn assert_pending_drains(agent: &str, home: &std::path::Path, data: &std::path::Path) {
    for _ in 0..10 {
        if !data.join(".ingest_pending.bin").exists() {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(300));
        ingest_into(agent, home, data, false);
    }
    assert!(!data.join(".ingest_pending.bin").exists());
}

#[test]
fn torture_claude() {
    torture_adapter(&TARGETS[0]);
}

#[test]
fn torture_codex() {
    torture_adapter(&TARGETS[1]);
}

#[test]
fn torture_kimi() {
    torture_adapter(&TARGETS[2]);
}

#[test]
fn torture_gemini() {
    torture_adapter(&TARGETS[3]);
}

#[test]
fn torture_cline() {
    torture_adapter(&TARGETS[4]);
}

/// Truncation sweep: cut the transcript at arbitrary byte offsets and require that the
/// parse (1) exits clean, (2) keeps the intake identity, and (3) emits only rows whose
/// (session, turn, text) exist in the full parse - truncation may lose the tail, never
/// hallucinate. Restoring the full bytes must converge back to the full output.
fn truncation_sweep(adapter: &'static str, seed_rel: &[&str], plant_rel: &[&str]) {
    let seed = fs::read(seed_path(&fixture_home(adapter), seed_rel)).unwrap();

    let make_home = |bytes: &[u8]| -> PathBuf {
        let home = temp_dir(&format!("sweep-home-{adapter}"));
        let p = seed_path(&home, plant_rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(&p, bytes).unwrap();
        home
    };

    let full_home = make_home(&seed);
    let full_data = temp_dir(&format!("sweep-full-{adapter}"));
    ingest_into(adapter, &full_home, &full_data, true);
    let full_keys = msg_keys(&full_data);
    let full_blob = normalize(&full_data);
    assert!(
        !full_keys.is_empty(),
        "{adapter}: sweep seed produced no rows"
    );

    let offsets = [
        1usize,
        seed.len() / 4,
        seed.len() / 2,
        seed.len() * 3 / 4,
        seed.len() - 1,
    ];
    for off in offsets {
        let home = make_home(&seed[..off]);
        let data = temp_dir(&format!("sweep-{adapter}-{off}"));
        ingest_into(adapter, &home, &data, true);
        check_intake_identity(&data);
        let keys = msg_keys(&data);
        let phantom: Vec<_> = keys.difference(&full_keys).collect();
        assert!(
            phantom.is_empty(),
            "{adapter}: truncation at byte {off} invented rows not in the full parse:\n{phantom:#?}"
        );
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }

    let restored_home = make_home(&seed);
    let restored_data = temp_dir(&format!("sweep-restored-{adapter}"));
    ingest_into(adapter, &restored_home, &restored_data, true);
    assert_eq!(
        normalize(&restored_data),
        full_blob,
        "{adapter}: re-ingest after restoring truncated bytes did not converge"
    );

    for p in [full_home, full_data, restored_home, restored_data] {
        let _ = fs::remove_dir_all(&p);
    }
}

#[test]
fn truncation_sweep_claude() {
    truncation_sweep(
        "claude",
        &[
            ".claude",
            "projects",
            "proj-alpha",
            "sess-claude-0001.jsonl",
        ],
        &[
            ".claude",
            "projects",
            "proj-alpha",
            "sess-claude-0001.jsonl",
        ],
    );
}

#[test]
fn truncation_sweep_codex() {
    truncation_sweep(
        "codex",
        &[
            ".codex",
            "sessions",
            "2026",
            "01",
            "02",
            "rollout-2026-01-02T10-00-00-22222222-2222-4222-8222-222222222222.jsonl",
        ],
        &[
            ".codex",
            "sessions",
            "2026",
            "01",
            "02",
            "rollout-2026-01-02T10-00-00-22222222-2222-4222-8222-222222222222.jsonl",
        ],
    );
}

/// A live writer's committed WAL row must be visible before checkpoint without blocking it.
#[test]
fn opencode_wal_commit_is_ingested_before_checkpoint() {
    let home = opencode_home();
    let db = home
        .join(".local")
        .join("share")
        .join("opencode")
        .join("opencode.db");
    let data = temp_dir("opencode-wal");

    ingest_into("opencode", &home, &data, false);
    assert!(normalize(&data).contains("convert config to yaml"));

    // switch to WAL and commit an edit WITHOUT checkpointing; -wal stays hot on disk
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.pragma_update(None, "journal_mode", "wal").unwrap();
    conn.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
    conn.execute(
        "UPDATE part SET data = '{\"type\":\"text\",\"text\":\"convert config to toml instead\"}' WHERE id = 'p1'",
        [],
    )
    .unwrap();

    ingest_into("opencode", &home, &data, false);
    let during = normalize(&data);
    assert!(
        during.contains("convert config to toml instead"),
        "committed WAL edit was omitted:\n{during}"
    );
    assert!(!during.contains("convert config to yaml"));

    conn.execute(
        "INSERT INTO part VALUES ('p-wal-2', 'm1', 'sess-oc-1', '{\"type\":\"text\",\"text\":\"writer remains live\"}', 1767348000002)",
        [],
    )
    .unwrap();
    ingest_into("opencode", &home, &data, false);
    assert!(normalize(&data).contains("writer remains live"));

    // SQLite text length/substr stop at NUL. Keep post-NUL Unicode in the public
    // ingest path so the event excerpt and both source units stay truthful.
    let long_output = format!("abc\0{}", "é".repeat(4_996));
    let tool = serde_json::json!({
        "type": "tool",
        "tool": "bash",
        "callID": "c1",
        "state": {"status": "completed", "input": {"command": "cat config.json"}, "output": long_output}
    });
    conn.execute(
        "UPDATE part SET data = ?1 WHERE id = 'p4'",
        [tool.to_string()],
    )
    .unwrap();
    ingest_into("opencode", &home, &data, false);
    let event_name = agrep_core::cache::event_fname("opencode", "sess-oc-1");
    let events = String::from_utf8(event_row(&data, &event_name).unwrap()).unwrap();
    let tool: serde_json::Value = events
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .find(|event: &serde_json::Value| event["call_id"] == "c1")
        .unwrap();
    assert_eq!(tool["output_chars"], 5_000);
    assert_eq!(tool["output_bytes"], long_output.len());
    assert_eq!(tool["output_truncated"], true);
    assert_eq!(
        tool["output"].as_str().unwrap().chars().count(),
        agrep_core::ingest::EVENT_CAP + 1
    );
    assert!(tool["output"].as_str().unwrap().starts_with("abc\0é"));

    conn.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))
        .unwrap();
    conn.close().unwrap();

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn opencode_malformed_matching_part_preserves_cold_publication() {
    let home = temp_dir("opencode-malformed-home");
    let store = home.join(".local/share/opencode");
    fs::create_dir_all(&store).unwrap();
    let db = rusqlite::Connection::open(store.join("opencode.db")).unwrap();
    db.execute_batch(
        r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
           CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
           CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
           INSERT INTO session VALUES ('s1', '/work/alpha', NULL, NULL, 1000);
           INSERT INTO message VALUES ('m1', 's1', '{"role":"user"}', 1000);
           INSERT INTO message VALUES ('m2', 's1', '{"role":"user"}', 2000);
           INSERT INTO message VALUES ('m3', 's1', '{"role":"user"}', 3000);
           INSERT INTO part VALUES ('p1', 'm1', 's1', '{"type":"text","text":"valid before"}', 1000);
           INSERT INTO part VALUES ('p2', 'm2', 's1', '{"type":"text","text":"broken"', 2000);
           INSERT INTO part VALUES ('p3', 'm3', 's1', '{"type":"text","text":"valid after"}', 3000);"#,
    )
    .unwrap();
    db.close().unwrap();

    let data = temp_dir("opencode-malformed-data");
    let output = ingest_output("opencode", &home, &data, true);
    assert!(
        output.status.success(),
        "cold ingest failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    check_intake_identity(&data);
    let rows = sorted_lines(&data.join("messages.jsonl"));
    assert_eq!(rows.len(), 2);
    assert!(rows.iter().any(|row| row.contains("valid before")));
    assert!(rows.iter().any(|row| row.contains("valid after")));

    let stats: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join("intake_stats.json")).unwrap()).unwrap();
    let entry = stats["files"]
        .as_object()
        .unwrap()
        .values()
        .find(|entry| entry["agent"] == "opencode")
        .unwrap();
    assert_eq!(entry["seen"], 3);
    assert_eq!(entry["rows"], 2);
    assert_eq!(entry["errors"], 1);
    assert!(entry["first_error"]
        .as_str()
        .unwrap()
        .contains("malformed part JSON: p2"));

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn opencode_type_errors_are_accounted_without_hiding_later_rows() {
    let home = temp_dir("opencode-type-error-home");
    let store = home.join(".local/share/opencode");
    fs::create_dir_all(&store).unwrap();
    let db = rusqlite::Connection::open(store.join("opencode.db")).unwrap();
    db.execute_batch(
        r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
           CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
           CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
           INSERT INTO session VALUES ('s1', '/work/alpha', NULL, NULL, 1000);
           INSERT INTO session VALUES ('child', '/work/alpha', 's1', 'child', 'bad-time');
           INSERT INTO message VALUES ('m1', 's1', '{"role":"user"}', 1000);
           INSERT INTO message VALUES ('m2', 's1', '{"role":123}', 2000);
           INSERT INTO message VALUES ('m3', 's1', '{"role":"user"}', 3000);
           INSERT INTO part VALUES ('p1', 'm1', 's1', '{"type":"text","text":"valid before"}', 1000);
           INSERT INTO part VALUES ('p2', 'm2', 's1', '{"type":"text","text":"bad role type"}', 2000);
           INSERT INTO part VALUES ('p3', 'm3', 's1', '{"type":"text","text":"valid after"}', 3000);"#,
    )
    .unwrap();
    db.close().unwrap();

    let data = temp_dir("opencode-type-error-data");
    let output = ingest_output("opencode", &home, &data, true);
    assert!(
        output.status.success(),
        "cold ingest failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    check_intake_identity(&data);
    let rows = sorted_lines(&data.join("messages.jsonl"));
    assert_eq!(rows.len(), 2);
    assert!(rows.iter().any(|row| row.contains("valid before")));
    assert!(rows.iter().any(|row| row.contains("valid after")));

    let stats: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join("intake_stats.json")).unwrap()).unwrap();
    let entry = stats["files"]
        .as_object()
        .unwrap()
        .values()
        .find(|entry| entry["agent"] == "opencode")
        .unwrap();
    assert_eq!(entry["seen"], 4);
    assert_eq!(entry["rows"], 2);
    assert_eq!(entry["errors"], 2);

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn opencode_child_query_schema_error_is_accounted() {
    let home = temp_dir("opencode-child-schema-home");
    let store = home.join(".local/share/opencode");
    fs::create_dir_all(&store).unwrap();
    let db = rusqlite::Connection::open(store.join("opencode.db")).unwrap();
    db.execute_batch(
        r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, time_created INTEGER);
           CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
           CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
           INSERT INTO session VALUES ('s1', '/work/alpha', NULL, 1000);
           INSERT INTO message VALUES ('m1', 's1', '{"role":"user"}', 1000);
           INSERT INTO part VALUES ('p1', 'm1', 's1', '{"type":"text","text":"still indexed"}', 1000);"#,
    )
    .unwrap();
    db.close().unwrap();

    let data = temp_dir("opencode-child-schema-data");
    let output = ingest_output("opencode", &home, &data, true);
    assert!(
        output.status.success(),
        "cold ingest failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    check_intake_identity(&data);
    let rows = sorted_lines(&data.join("messages.jsonl"));
    assert_eq!(rows.len(), 1);
    assert!(rows[0].contains("still indexed"));

    let stats: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join("intake_stats.json")).unwrap()).unwrap();
    let entry = stats["files"]
        .as_object()
        .unwrap()
        .values()
        .find(|entry| entry["agent"] == "opencode")
        .unwrap();
    assert_eq!(entry["seen"], 2);
    assert_eq!(entry["rows"], 1);
    assert_eq!(entry["errors"], 1);
    assert!(entry["first_error"]
        .as_str()
        .unwrap()
        .contains("session query"));

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn corrupt_crush_registry_still_publishes_standalone_database() {
    let home = crush_home();
    let root = home.join(".local").join("share").join("crush");
    fs::write(root.join("projects.json"), b"{broken").unwrap();
    let data = temp_dir("crush-corrupt-registry");

    let output = ingest_output("crush", &home, &data, true);
    assert!(
        output.status.success(),
        "healthy standalone Crush DB was blocked:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let published = normalize(&data);
    assert!(
        published.contains("convert the readme to asciidoc"),
        "standalone Crush rows were not published:\n{published}"
    );
    assert!(!data.join(".source_snapshot.bin").exists());

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn corrupt_crush_registry_without_healthy_database_does_not_publish() {
    let home = temp_dir("crush-corrupt-only-home");
    let root = home.join(".local").join("share").join("crush");
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("projects.json"), b"{broken").unwrap();
    let data = temp_dir("crush-corrupt-only-data");

    let output = ingest_output("crush", &home, &data, true);
    assert!(!output.status.success());
    assert!(!data.join("messages.jsonl").exists());
    assert!(!data.join(".ingest_cache.bin").exists());

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn crush_wal_commit_invalidates_tokens_without_session_timestamp() {
    let home = crush_home();
    let db = home
        .join(".local")
        .join("share")
        .join("crush")
        .join("crush.db");
    let data = temp_dir("crush-wal");

    ingest_into("crush", &home, &data, false);
    assert!(normalize(&data).contains("convert the readme to asciidoc"));
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.pragma_update(None, "journal_mode", "wal").unwrap();
    conn.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
    conn.execute(
        "UPDATE messages SET parts = '[{\"type\":\"text\",\"data\":{\"text\":\"convert the readme to markdown\"}}]' WHERE id = 'm1'",
        [],
    )
    .unwrap();

    ingest_into("crush", &home, &data, false);
    let during = normalize(&data);
    assert!(
        during.contains("convert the readme to markdown"),
        "committed Crush WAL edit was omitted:\n{during}"
    );
    assert!(!during.contains("convert the readme to asciidoc"));
    conn.execute(
        "UPDATE sessions SET title = 'writer-still-live' WHERE id = 'sc1'",
        [],
    )
    .unwrap();

    drop(conn);
    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn crush_live_writer_cannot_roll_back_a_published_token_generation() {
    let home = crush_home();
    let db = home.join(".local/share/crush/crush.db");
    let data = temp_dir("crush-live-generation");
    ingest_into("crush", &home, &data, false);

    let marker = "live token generation marker 8d2c";
    let event_marker = "live token event marker 8d2c";
    let connection = rusqlite::Connection::open(&db).unwrap();
    connection
        .pragma_update(None, "journal_mode", "wal")
        .unwrap();
    connection
        .pragma_update(None, "wal_autocheckpoint", 0)
        .unwrap();
    connection
        .execute(
            "INSERT INTO sessions VALUES ('sc-live', NULL, 'live', 1767350000000, 1767350000000)",
            [],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO messages VALUES ('m-live-user','sc-live','user',?1,NULL,1767350000000,1767350000000)",
            [serde_json::json!([{"type":"text","data":{"text":marker}}]).to_string()],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO messages VALUES ('m-live-agent','sc-live','assistant',?1,'gpt-5.5',1767350001000,1767350001000)",
            [serde_json::json!([
                {"type":"text","data":{"text":"running live command"}},
                {"type":"tool_call","data":{"id":"tc-live-8d2c","name":"bash","input":{
                    "command":format!("echo {event_marker}")
                },"finished":true}},
                {"type":"tool_result","data":{"tool_call_id":"tc-live-8d2c","name":"bash",
                    "content":event_marker,"is_error":false}}
            ]).to_string()],
        )
        .unwrap();
    connection.close().unwrap();

    let running = Arc::new(AtomicBool::new(true));
    let ready = Arc::new(Barrier::new(2));
    let writer_running = Arc::clone(&running);
    let writer_ready = Arc::clone(&ready);
    let writer_db = db.clone();
    let writer = std::thread::spawn(move || {
        let connection = rusqlite::Connection::open(writer_db).unwrap();
        connection
            .busy_timeout(std::time::Duration::from_secs(2))
            .unwrap();
        writer_ready.wait();
        let mut sequence = 0u64;
        while writer_running.load(Ordering::Acquire) {
            let _ = connection.execute(
                "UPDATE sessions SET title=?1 WHERE id='sc1'",
                [format!("live-writer-{sequence}")],
            );
            sequence += 1;
            std::thread::sleep(std::time::Duration::from_millis(8));
        }
    });
    ready.wait();
    std::thread::sleep(std::time::Duration::from_millis(5));

    let mut published = false;
    let mut last_error = String::new();
    for _ in 0..160 {
        let output = ingest_output("crush", &home, &data, false);
        last_error = String::from_utf8_lossy(&output.stderr).into_owned();
        let state = normalize(&data);
        let has_message = state.contains(marker);
        let has_event = state.contains(event_marker);
        assert_eq!(
            has_message, has_event,
            "moving token generation split message/event publication"
        );
        if output.status.success() && has_message {
            published = true;
            break;
        }
    }
    if !published {
        running.store(false, Ordering::Release);
        writer.join().unwrap();
        panic!("live generation never published: {last_error}");
    }

    for _ in 0..12 {
        let _ = ingest_output("crush", &home, &data, false);
        let state = normalize(&data);
        assert!(
            state.contains(marker),
            "published message generation rolled back"
        );
        assert!(
            state.contains(event_marker),
            "published event generation rolled back"
        );
    }
    running.store(false, Ordering::Release);
    writer.join().unwrap();
    ingest_into("crush", &home, &data, false);
    assert_pending_drains("crush", &home, &data);

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn cursor_wal_commit_is_ingested_and_workspace_move_relabels_cache() {
    let home = cursor_home();
    let user = home.join(".config").join("Cursor").join("User");
    let db = user.join("globalStorage").join("state.vscdb");
    let workspace = user
        .join("workspaceStorage")
        .join("aaaa0001bbbb0002cccc0003dddd0004")
        .join("workspace.json");
    let data = temp_dir("cursor-wal-mapping");

    ingest_into("cursor", &home, &data, false);
    let initial = normalize(&data);
    assert!(initial.contains("the login test fails every third run"));
    assert!(initial.contains(r#""project":"flaky-app""#));

    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.pragma_update(None, "journal_mode", "wal").unwrap();
    conn.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
    conn.execute(
        "UPDATE cursorDiskKV SET value = ?1 WHERE key = ?2",
        rusqlite::params![
            r#"{"bubbleId":"b1b11111-1111-4111-8111-111111111111","type":1,"text":"the login test now fails every fourth run","createdAt":"2026-03-01T12:00:00.000Z"}"#,
            "bubbleId:c1a11111-1111-4111-8111-111111111111:b1b11111-1111-4111-8111-111111111111"
        ],
    )
    .unwrap();
    ingest_into("cursor", &home, &data, false);
    assert!(normalize(&data).contains("the login test now fails every fourth run"));

    fs::write(
        workspace,
        r#"{"folder":"file:///c%3A/Users/tester/Desktop/renamed-app"}"#,
    )
    .unwrap();
    ingest_into("cursor", &home, &data, false);
    let relabeled = normalize(&data);
    assert!(relabeled.contains(r#""project":"renamed-app""#));
    assert!(!relabeled.contains(r#""project":"flaky-app""#));
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES ('writer-proof', 'still live')",
        [],
    )
    .unwrap();

    drop(conn);
    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn missing_store_requires_a_repeated_absent_generation() {
    let home = temp_dir("missing-root-home");
    copy_dir(&fixture_home("claude"), &home);
    let root = home.join(".claude").join("projects");
    let parked = home.join("projects-unmounted");
    let data = temp_dir("missing-root-data");

    ingest_into("claude", &home, &data, false);
    let baseline = normalize(&data);
    fs::rename(&root, &parked).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(
        normalize(&data),
        baseline,
        "one absence deleted a transient store"
    );
    fs::rename(&parked, &root).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(
        normalize(&data),
        baseline,
        "restored store did not converge"
    );

    fs::remove_dir_all(&root).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(
        normalize(&data),
        baseline,
        "first stable absence deleted the store"
    );
    ingest_into("claude", &home, &data, false);
    let deleted = normalize(&data);
    assert!(!deleted.contains("flaky timer test"));
    assert!(deleted.contains("=== messages.jsonl (0 lines) ==="));
    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("unchanged since last index"));

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn missing_token_store_also_requires_repeated_absence() {
    let home = cursor_home();
    let db = home
        .join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb");
    let data = temp_dir("missing-token-store-data");

    ingest_into("cursor", &home, &data, false);
    let baseline = normalize(&data);
    assert!(baseline.contains("the login test fails every third run"));
    fs::remove_file(db).unwrap();
    ingest_into("cursor", &home, &data, false);
    assert_eq!(normalize(&data), baseline);
    ingest_into("cursor", &home, &data, false);
    assert!(normalize(&data).contains("=== messages.jsonl (0 lines) ==="));

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

/// Filesystem change identity closes the common `(mtime, size)` blind spot without reading every
/// transcript on journaled Windows volumes or Unix filesystems.
#[cfg(any(unix, windows))]
#[test]
fn stat_cache_detects_same_size_edit_with_restored_mtime() {
    let home = temp_dir("blindspot-home");
    copy_dir(&fixture_home("claude"), &home);
    let file = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    let data = temp_dir("blindspot-data");

    ingest_into("claude", &home, &data, false);
    let before = normalize(&data);
    assert!(
        before.contains("flaky timer test"),
        "fixture didn't ingest:\n{before}"
    );

    // same byte length ("test" -> "zest"), mtime restored: the freshness key is frozen
    let mtime = fs::metadata(&file).unwrap().modified().unwrap();
    let body = fs::read_to_string(&file)
        .unwrap()
        .replace("flaky timer test", "flaky timer zest");
    fs::write(&file, body).unwrap();
    fs::File::options()
        .write(true)
        .open(&file)
        .unwrap()
        .set_modified(mtime)
        .unwrap();

    ingest_into("claude", &home, &data, false);
    let during = normalize(&data);
    assert!(
        during.contains("flaky timer zest") && !during.contains("flaky timer test"),
        "warm ingest missed a same-size edit whose mtime was restored:\n{during}"
    );
    assert_ne!(before, during);

    ingest_into("claude", &home, &data, true);
    let after = normalize(&data);
    assert!(
        after.contains("flaky timer zest") && !after.contains("flaky timer test"),
        "--full did not preserve the repaired source state:\n{after}"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A source can change only tool events while leaving every Message field byte-identical.
/// The message-signature shortcut must still publish the event delta and its changed-session
/// marker before recording the new source snapshot.
#[test]
fn event_only_change_survives_message_signature_skip() {
    let home = temp_dir("event-only-home");
    copy_dir(&fixture_home("claude"), &home);
    let file = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    let data = temp_dir("event-only-data");

    ingest_into("claude", &home, &data, false);
    let messages = fs::read(data.join("messages.jsonl")).unwrap();
    let replies = fs::read(data.join("replies.jsonl")).unwrap();
    let sessions = fs::read(data.join("sessions.jsonl")).unwrap();
    let families = fs::read(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap();
    fs::remove_file(data.join(".changed_sessions")).unwrap();

    let body = fs::read_to_string(&file)
        .unwrap()
        .replace("3 passed; 1 failed", "all four tests passed successfully");
    fs::write(&file, body).unwrap();
    ingest_into("claude", &home, &data, false);

    assert_eq!(messages, fs::read(data.join("messages.jsonl")).unwrap());
    assert_eq!(replies, fs::read(data.join("replies.jsonl")).unwrap());
    assert_eq!(sessions, fs::read(data.join("sessions.jsonl")).unwrap());
    assert_eq!(
        families,
        fs::read(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap()
    );
    let published = normalize(&data);
    assert!(
        published.contains("all four tests passed successfully"),
        "event delta was skipped:\n{published}"
    );
    assert!(
        !published.contains("3 passed; 1 failed"),
        "stale event survived:\n{published}"
    );
    let changed = fs::read_to_string(data.join(".changed_sessions")).unwrap();
    assert!(changed
        .lines()
        .any(|s| s == "11111111-1111-4111-8111-111111111111"));
    assert!(data.join(".source_snapshot.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn message_only_delta_preserves_proven_event_generation() {
    let home = temp_dir("message-only-event-proof-home");
    copy_dir(&fixture_home("claude"), &home);
    let source = claude_source(&home);
    let data = temp_dir("message-only-event-proof-data");
    ingest_into("claude", &home, &data, false);

    let proof_path = data.join(".events_complete.claude.json");
    let generation_path = data
        .join("events")
        .join(agrep_core::cache::EVENT_GENERATION_NAME);
    let proof_before = fs::read(&proof_path).unwrap();
    let generation_before = fs::read(&generation_path).unwrap();
    let events_before = event_rows(&data);
    let body = fs::read_to_string(&source).unwrap().replace(
        "how do i fix the flaky timer test",
        "how should i repair the flaky timer test",
    );
    fs::write(&source, body).unwrap();

    ingest_into("claude", &home, &data, false);
    assert!(normalize(&data).contains("how should i repair the flaky timer test"));
    assert_eq!(fs::read(&proof_path).unwrap(), proof_before);
    assert_eq!(fs::read(&generation_path).unwrap(), generation_before);
    assert_eq!(event_rows(&data), events_before);
    let warm = ingest_output("claude", &home, &data, false);
    assert!(String::from_utf8_lossy(&warm.stdout).contains("skipped ingest + writes"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn warm_event_delta_refreshes_aggregate_stats() {
    let home = temp_dir("event-stats-delta-home");
    copy_dir(&fixture_home("gemini"), &home);
    let source = home
        .join(".gemini")
        .join("tmp")
        .join("hash5555synthetic")
        .join("chats")
        .join("session-2026-01-02T10-00-55555555.json");
    let data = temp_dir("event-stats-delta-data");
    ingest_into("gemini", &home, &data, false);
    let before: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join("event_stats.json")).unwrap()).unwrap();
    assert_eq!(before["total"], 1);

    let mut session: serde_json::Value =
        serde_json::from_slice(&fs::read(&source).unwrap()).unwrap();
    session["messages"][4]["toolCalls"] = serde_json::json!([{
        "id": "shell-2",
        "name": "shell",
        "args": {"command": "cargo test"},
        "result": [{"functionResponse": {
            "id": "shell-2",
            "name": "shell",
            "response": {"output": "ok"}
        }}],
        "status": "success"
    }]);
    fs::write(&source, serde_json::to_vec_pretty(&session).unwrap()).unwrap();
    ingest_into("gemini", &home, &data, false);

    let after: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join("event_stats.json")).unwrap()).unwrap();
    assert_eq!(after["total"], 2);
    assert_eq!(after["by_agent"]["gemini"]["calls"], 2);
    let warm = ingest_output("gemini", &home, &data, false);
    assert!(String::from_utf8_lossy(&warm.stdout).contains("skipped ingest + writes"));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

fn claude_source(home: &Path) -> PathBuf {
    home.join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl")
}

fn corrupt_current_cache_payload(data: &Path) -> Vec<u8> {
    let path = data.join(".ingest_cache.bin");
    let mut body = fs::read(&path).unwrap();
    assert!(
        body.len() > 44,
        "current cache is too short to retain its writing-build header"
    );
    body.truncate(44);
    body.extend_from_slice(b"deliberately corrupt cache payload");
    fs::write(path, &body).unwrap();
    body
}

fn force_ambiguous_event_recovery(data: &Path) {
    corrupt_current_cache_payload(data);
    fs::remove_file(data.join(".events_complete.claude.json")).unwrap();
}

/// Event recovery with no cache at all. Unlike an undecodable payload this is not a broken
/// artifact agrep can prove it owns, so it keeps the two-stable-snapshot recovery protocol.
fn force_cacheless_event_recovery(data: &Path) {
    fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
    let _ = fs::remove_file(data.join(".ingest_cache.bin.journal"));
    fs::remove_file(data.join(".events_complete.claude.json")).unwrap();
}

fn assert_stable_retry_refusal(output: &std::process::Output) {
    assert!(!output.status.success(), "recovery unexpectedly succeeded");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("two stable source snapshots"),
        "unexpected failure: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A discarded base has no rows to serve, so material the published generation held may not
/// vanish on one observation: the pass retains the old generation and retries instead.
fn assert_retained_generation_refusal(output: &std::process::Output) {
    assert!(!output.status.success(), "recovery unexpectedly succeeded");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("retained the old generation"),
        "unexpected failure: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A never-installed adapter is a clean absence from the first preflight onward: it must not
/// produce durable source health, and the Rust store census must stay empty.
#[test]
fn never_installed_store_is_cleanly_absent_from_ingest_and_store_census() {
    use std::process::Command;

    let home = temp_dir("never-installed-store-home");
    let data = temp_dir("never-installed-store-data");
    assert!(!home.join(".cline").exists());

    ingest_into("cline", &home, &data, false);
    assert!(
        fs::read_to_string(data.join("messages.jsonl"))
            .unwrap()
            .is_empty(),
        "missing Cline store unexpectedly produced indexed rows"
    );
    assert!(!data.join(".source-health.json").exists());

    let mut stores = Command::new(BIN);
    stores.arg("stores");
    stores.env("AGREP_HOME", &home);
    stores.env("AGREP_DATA_DIR", &data);
    for key in [
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "CLINE_DIR",
        "CRUSH_GLOBAL_DATA",
        "LOCALAPPDATA",
        "OPENCODE_DB",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "AGREP_RS_BIN",
    ] {
        stores.env_remove(key);
    }
    let stores = stores.output().unwrap();
    assert!(
        stores.status.success(),
        "{}",
        String::from_utf8_lossy(&stores.stderr)
    );
    let rows: serde_json::Value = serde_json::from_slice(&stores.stdout).unwrap();
    assert_eq!(rows, serde_json::json!([]));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A foreign family whose daemon lock is still present keeps the R8 read-only serve:
/// takeover requires a clear daemon fence, so a held (live or not-yet-reclaimed) owner
/// record preserves every byte of the old build's stores.
#[test]
fn live_fenced_foreign_owner_returns_before_lock_sweep_or_publication() {
    use std::process::Command;

    fn run(home: &Path, data: &Path, build_id: &str) -> std::process::Output {
        let mut command = Command::new(BIN);
        command.args(["index", "--agent", "claude", "--full"]);
        command.env("AGREP_HOME", home);
        command.env("AGREP_DATA_DIR", data);
        command.env("AGREP_RUNTIME_BUILD_ID", build_id);
        for key in [
            "USERPROFILE",
            "HOME",
            "APPDATA",
            "CLINE_DIR",
            "CRUSH_GLOBAL_DATA",
            "LOCALAPPDATA",
            "OPENCODE_DB",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "AGREP_RS_BIN",
        ] {
            command.env_remove(key);
        }
        command.output().expect("spawn owned agrep-rs")
    }

    fn snapshot(data: &Path) -> Vec<(String, Vec<u8>, std::time::SystemTime)> {
        [
            ".derived-owner.json",
            ".derived_generation.json",
            ".ingest.sig",
            ".ingest_cache.bin",
            ".source_snapshot.bin",
            "intake_stats.json",
            "messages.jsonl",
            "replies.jsonl",
            "sessions.jsonl",
        ]
        .into_iter()
        .map(|name| {
            let path = data.join(name);
            (
                name.to_string(),
                fs::read(&path).unwrap(),
                fs::metadata(&path).unwrap().modified().unwrap(),
            )
        })
        .collect()
    }

    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("foreign-owner-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("foreign-owner-data");

    let first = run(&home, &data, owner_a);
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let owner: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap()).unwrap();
    assert_eq!(owner["build_id"], owner_a);
    let before = snapshot(&data);

    let new_source = home
        .join(".claude")
        .join("projects")
        .join("foreign-owner-project")
        .join("new-session.jsonl");
    fs::create_dir_all(new_source.parent().unwrap()).unwrap();
    fs::write(
        &new_source,
        concat!(
            "{\"type\":\"user\",\"sessionId\":\"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb\",",
            "\"message\":{\"role\":\"user\",\"content\":\"must stay unindexed\"}}\n",
        ),
    )
    .unwrap();
    let dead_staging = data.join("messages.jsonl.tmp.999999.1");
    fs::write(&dead_staging, b"must not be swept by a foreign build").unwrap();
    fs::write(
        data.join(".indexd.v2.lock"),
        format!(
            "pid={} start=unknown protocol=2 package=x build={owner_a} group=1 token=aa time=1\n",
            std::process::id()
        ),
    )
    .unwrap();

    let second = run(&home, &data, owner_b);
    assert!(
        second.status.success(),
        "{}",
        String::from_utf8_lossy(&second.stderr)
    );
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(stderr.contains(&format!("owned-by {owner_a}")), "{stderr}");
    assert!(
        stderr.contains("serving the published snapshot read-only"),
        "{stderr}"
    );
    assert!(!data.join(".index.lock").exists());
    assert!(
        dead_staging.exists(),
        "foreign build crossed the staging sweep"
    );
    assert_eq!(snapshot(&data), before);
    assert!(!fs::read_to_string(data.join("messages.jsonl"))
        .unwrap()
        .contains("must stay unindexed"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn live_fenced_foreign_database_owner_fences_ownerless_missing_or_legacy_cache() {
    use std::process::Command;

    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("foreign-db-owner-home");
    copy_dir(&fixture_home("claude"), &home);

    for cache_shape in ["missing", "legacy"] {
        let data = temp_dir(&format!("foreign-db-owner-{cache_shape}-data"));
        let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
        connection
            .execute_batch(&format!(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                 INSERT INTO meta(key, value) VALUES('build_id', '{owner_a}');"
            ))
            .unwrap();
        drop(connection);
        fs::write(
            data.join("messages.jsonl"),
            b"{\"text\":\"published owner-A snapshot\"}\n",
        )
        .unwrap();
        fs::write(data.join("replies.jsonl"), b"").unwrap();
        fs::write(data.join("sessions.jsonl"), b"").unwrap();
        if cache_shape == "legacy" {
            fs::write(data.join(".ingest_cache.bin"), b"legacy ownerless cache").unwrap();
        }
        let dead_staging = data.join("messages.jsonl.tmp.999999.1");
        fs::write(&dead_staging, b"must survive the foreign preflight").unwrap();
        fs::write(
            data.join(".indexd.v2.lock"),
            format!(
                "pid={} start=unknown protocol=2 package=x build={owner_a} group=1 token=aa time=1\n",
                std::process::id()
            ),
        )
        .unwrap();
        let mut before = fs::read_dir(&data)
            .unwrap()
            .map(|entry| {
                let path = entry.unwrap().path();
                (
                    path.file_name().unwrap().to_string_lossy().into_owned(),
                    fs::read(&path).unwrap(),
                    fs::metadata(&path).unwrap().modified().unwrap(),
                )
            })
            .collect::<Vec<_>>();
        before.sort_by(|left, right| left.0.cmp(&right.0));

        let mut command = Command::new(BIN);
        command.args(["index", "--agent", "claude", "--full"]);
        command.env("AGREP_HOME", &home);
        command.env("AGREP_DATA_DIR", &data);
        command.env("AGREP_RUNTIME_BUILD_ID", owner_b);
        for key in [
            "USERPROFILE",
            "HOME",
            "APPDATA",
            "CLINE_DIR",
            "CRUSH_GLOBAL_DATA",
            "LOCALAPPDATA",
            "OPENCODE_DB",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "AGREP_RS_BIN",
        ] {
            command.env_remove(key);
        }
        let output = command.output().expect("spawn foreign DB reader");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(output.status.success(), "{stderr}");
        assert!(
            stderr.contains(&format!("corpus.db owned-by {owner_a}")),
            "{stderr}"
        );
        assert!(
            stderr.contains("serving the published snapshot read-only"),
            "{stderr}"
        );
        assert!(!data.join(".index.lock").exists());
        assert!(!data.join(".derived-owner.json").exists());
        assert!(dead_staging.exists());

        let mut after = fs::read_dir(&data)
            .unwrap()
            .map(|entry| {
                let path = entry.unwrap().path();
                (
                    path.file_name().unwrap().to_string_lossy().into_owned(),
                    fs::read(&path).unwrap(),
                    fs::metadata(&path).unwrap().modified().unwrap(),
                )
            })
            .collect::<Vec<_>>();
        after.sort_by(|left, right| left.0.cmp(&right.0));
        assert_eq!(after, before, "{cache_shape} cache crossed the write fence");

        let _ = fs::remove_dir_all(data);
    }
    let _ = fs::remove_dir_all(home);
}

/// Upgrade day: every derived store names an absent build and no daemon lock is held. One
/// `agrep index` from the successor collapses all three ownership gates - anchor, parse
/// cache, corpus.db - in a single invocation: it discloses the takeover, rebuilds the
/// derived stores, republishes ownership, and preserves the published transcripts.
#[test]
fn dead_foreign_owner_takeover_converges_in_one_invocation() {
    use std::process::Command;

    fn run(home: &Path, data: &Path, build_id: &str, full: bool) -> std::process::Output {
        let mut command = Command::new(BIN);
        command.args(["index", "--agent", "claude"]);
        if full {
            command.arg("--full");
        }
        command.env("AGREP_HOME", home);
        command.env("AGREP_DATA_DIR", data);
        command.env("AGREP_RUNTIME_BUILD_ID", build_id);
        for key in [
            "USERPROFILE",
            "HOME",
            "APPDATA",
            "CLINE_DIR",
            "CRUSH_GLOBAL_DATA",
            "LOCALAPPDATA",
            "OPENCODE_DB",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "AGREP_RS_BIN",
        ] {
            command.env_remove(key);
        }
        command.output().expect("spawn owned agrep-rs")
    }

    fn forge_foreign_corpus(data: &Path, owner: &str) {
        let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
        let exists: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name='msgs'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        if exists == 0 {
            connection
                .execute_batch(
                    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                     CREATE TABLE msgs(
                         id INTEGER PRIMARY KEY, session TEXT NOT NULL, turn INTEGER, ts INTEGER,
                         agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
                         who TEXT, text TEXT,
                         fts_text TEXT CHECK(
                             fts_text IS NULL OR instr(fts_text, char(0)) = 0),
                         content_digest TEXT CHECK(
                             content_digest IS NULL OR
                             (length(content_digest) = 4 AND
                              content_digest NOT GLOB '*[^0-9a-f]*')));
                     CREATE INDEX msgs_session ON msgs(session, turn);
                     CREATE INDEX msgs_transcript_session_turn ON msgs(session, turn)
                         WHERE who <> 'tool';
                     CREATE INDEX msgs_who_ts ON msgs(who, coalesce(ts, 0) DESC);
                     CREATE INDEX msgs_re_i_exceptions ON msgs(id) WHERE
                         instr(text, 'İ') > 0 OR instr(text, 'ı') > 0
                         OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0;
                     CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT);
                     CREATE TABLE session_family(
                         session TEXT PRIMARY KEY, root TEXT NOT NULL,
                         side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID;
                     CREATE INDEX session_family_root ON session_family(root);
                     CREATE TABLE boundary_stats(
                         token TEXT PRIMARY KEY, n INTEGER NOT NULL, s INTEGER NOT NULL,
                         q INTEGER NOT NULL) WITHOUT ROWID;
                     INSERT INTO msgs(session, who, text)
                         VALUES('session', 'user', 'takeovertortureneedle');
                     CREATE VIEW msgs_fts_content AS
                         SELECT id, coalesce(fts_text, text) AS text FROM msgs;
                     CREATE VIEW msgs_prose_fts_content AS
                         SELECT id, coalesce(fts_text, text) AS text
                         FROM msgs WHERE who <> 'tool';
                     CREATE VIRTUAL TABLE msgs_fts USING fts5(
                         text, content='msgs_fts_content', content_rowid='id', tokenize='trigram');
                     CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
                         text, content='msgs_prose_fts_content', content_rowid='id',
                         tokenize='trigram');
                     INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild');
                     INSERT INTO msgs_prose_fts(rowid, text)
                         SELECT id, coalesce(fts_text, text) FROM msgs;
                     CREATE TRIGGER msgs_ai AFTER INSERT ON msgs BEGIN
                         INSERT INTO msgs_fts(rowid, text)
                             VALUES(new.id, coalesce(new.fts_text, new.text)); END;
                     CREATE TRIGGER msgs_ad AFTER DELETE ON msgs BEGIN
                         INSERT INTO msgs_fts(msgs_fts, rowid, text)
                             VALUES('delete', old.id, coalesce(old.fts_text, old.text)); END;
                     CREATE TRIGGER msgs_au AFTER UPDATE OF text, fts_text ON msgs
                     WHEN coalesce(old.fts_text, old.text)
                          IS NOT coalesce(new.fts_text, new.text) BEGIN
                         INSERT INTO msgs_fts(msgs_fts, rowid, text)
                             VALUES('delete', old.id, coalesce(old.fts_text, old.text));
                         INSERT INTO msgs_fts(rowid, text)
                             VALUES(new.id, coalesce(new.fts_text, new.text)); END;
                     CREATE TRIGGER msgs_prose_ai AFTER INSERT ON msgs
                     WHEN new.who <> 'tool' BEGIN
                         INSERT INTO msgs_prose_fts(rowid, text)
                             VALUES(new.id, coalesce(new.fts_text, new.text)); END;
                     CREATE TRIGGER msgs_prose_ad AFTER DELETE ON msgs
                     WHEN old.who <> 'tool' BEGIN
                         INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                             VALUES('delete', old.id, coalesce(old.fts_text, old.text)); END;
                     CREATE TRIGGER msgs_prose_au_old AFTER UPDATE OF text, fts_text, who ON msgs
                     WHEN old.who <> 'tool'
                          AND (coalesce(old.fts_text, old.text)
                               IS NOT coalesce(new.fts_text, new.text)
                               OR new.who = 'tool') BEGIN
                         INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                             VALUES('delete', old.id, coalesce(old.fts_text, old.text));
                         INSERT INTO msgs_prose_fts(rowid, text)
                             SELECT new.id, coalesce(new.fts_text, new.text)
                             WHERE new.who <> 'tool'; END;
                     CREATE TRIGGER msgs_prose_au_new AFTER UPDATE OF text, fts_text, who ON msgs
                     WHEN old.who = 'tool' AND new.who <> 'tool' BEGIN
                         INSERT INTO msgs_prose_fts(rowid, text)
                             VALUES(new.id, coalesce(new.fts_text, new.text)); END;",
                )
                .unwrap();
        }
        connection
            .execute(
                "INSERT INTO meta(key, value) VALUES('build_id', ?1)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [owner],
            )
            .unwrap();
        for (key, value) in [
            ("schema", "15"),
            ("stamp", "takeover-torture-stamp"),
            ("fts_triggers", "4"),
        ] {
            connection
                .execute(
                    "INSERT INTO meta(key, value) VALUES(?1, ?2)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [key, value],
                )
                .unwrap();
        }
    }

    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("dead-owner-takeover-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("dead-owner-takeover-data");

    let first = run(&home, &data, owner_a, true);
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    forge_foreign_corpus(&data, owner_a);
    let transcripts = ["messages.jsonl", "replies.jsonl", "sessions.jsonl"].map(|name| {
        let path = data.join(name);
        (
            fs::read(&path).unwrap(),
            fs::metadata(&path).unwrap().modified().unwrap(),
        )
    });

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(stderr.contains(&format!("owned-by {owner_a}")), "{stderr}");
    assert!(
        stderr.contains("took over") && stderr.contains("published transcripts kept"),
        "{stderr}"
    );
    assert!(
        !stderr.contains("serving the published snapshot read-only"),
        "{stderr}"
    );
    let owner: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap()).unwrap();
    assert_eq!(owner["build_id"], owner_b);
    assert!(
        data.join("corpus.db").exists(),
        "a verified foreign corpus.db is adopted, never rebuilt"
    );
    assert_eq!(
        rusqlite::Connection::open(data.join("corpus.db"))
            .unwrap()
            .query_row(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH 'takeovertortureneedle'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
        1
    );
    let after = ["messages.jsonl", "replies.jsonl", "sessions.jsonl"].map(|name| {
        let path = data.join(name);
        (
            fs::read(&path).unwrap(),
            fs::metadata(&path).unwrap().modified().unwrap(),
        )
    });
    assert_eq!(
        after, transcripts,
        "takeover touched the published transcripts"
    );

    let third = run(&home, &data, owner_b, false);
    assert!(
        String::from_utf8_lossy(&third.stdout).contains("unchanged since last index"),
        "{}",
        String::from_utf8_lossy(&third.stdout)
    );
    assert!(!String::from_utf8_lossy(&third.stderr).contains("took over"));

    // The --full flank goes through the same seam: reset to a foreign family and converge.
    fs::write(
        data.join(".derived-owner.json"),
        format!("{{\"version\":1,\"build_id\":\"{owner_a}\"}}"),
    )
    .unwrap();
    forge_foreign_corpus(&data, owner_a);
    let full = run(&home, &data, owner_b, true);
    let stderr = String::from_utf8_lossy(&full.stderr);
    assert!(full.status.success(), "{stderr}");
    assert!(stderr.contains("took over"), "{stderr}");
    let owner: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap()).unwrap();
    assert_eq!(owner["build_id"], owner_b);

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn live_daemon_descriptor_fences_first_adoption_unless_exactly_authorized() {
    use std::process::Command;

    fn run(
        home: &Path,
        data: &Path,
        build: &str,
        adoption_token: Option<&str>,
        identity_blocked: bool,
    ) -> std::process::Output {
        let mut command = Command::new(BIN);
        command.args(["index", "--agent", "claude"]);
        command.env("AGREP_HOME", home);
        command.env("AGREP_DATA_DIR", data);
        command.env("AGREP_RUNTIME_BUILD_ID", build);
        if let Some(token) = adoption_token {
            command.env("AGREP_DERIVED_ADOPTION_OWNER_TOKEN", token);
        } else {
            command.env_remove("AGREP_DERIVED_ADOPTION_OWNER_TOKEN");
        }
        if identity_blocked {
            command.env(
                "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
                "Python runtime moved",
            );
        } else {
            command.env_remove("AGREP_DERIVED_WRITER_IDENTITY_BLOCKED");
        }
        for key in [
            "USERPROFILE",
            "HOME",
            "APPDATA",
            "CLINE_DIR",
            "CRUSH_GLOBAL_DATA",
            "LOCALAPPDATA",
            "OPENCODE_DB",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "AGREP_RS_BIN",
        ] {
            command.env_remove(key);
        }
        command.output().expect("spawn adoption-fenced agrep-rs")
    }

    let build = "bbbbbbbbbbbbbbbbbbbb";
    let token = "cccccccccccccccccccccccccccccccc";
    let home = temp_dir("daemon-adoption-home");
    copy_dir(&fixture_home("claude"), &home);

    let blocked = temp_dir("legacy-daemon-adoption-data");
    let legacy_owner = blocked.join(".indexd.lock");
    fs::write(
        &legacy_owner,
        b"pid=1 start=legacy protocol=1 package=0.2.0 build=legacy\n",
    )
    .unwrap();
    let dead_staging = blocked.join("messages.jsonl.tmp.999999.1");
    fs::write(&dead_staging, b"must not be swept").unwrap();
    let before = (
        fs::read(&legacy_owner).unwrap(),
        fs::metadata(&legacy_owner).unwrap().modified().unwrap(),
        fs::read(&dead_staging).unwrap(),
        fs::metadata(&dead_staging).unwrap().modified().unwrap(),
    );
    let refused = run(&home, &blocked, build, None, false);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(refused.status.success(), "{stderr}");
    assert!(
        stderr.contains("legacy or ambiguous freshness-daemon ownership"),
        "{stderr}"
    );
    assert!(stderr.contains("serving the published snapshot read-only"));
    assert!(!blocked.join(".index.lock").exists());
    assert!(!blocked.join(".derived-owner.json").exists());
    assert_eq!(
        (
            fs::read(&legacy_owner).unwrap(),
            fs::metadata(&legacy_owner).unwrap().modified().unwrap(),
            fs::read(&dead_staging).unwrap(),
            fs::metadata(&dead_staging).unwrap().modified().unwrap(),
        ),
        before
    );

    let authorized = temp_dir("current-daemon-adoption-data");
    fs::write(
        authorized.join(".indexd.v2.lock"),
        format!(
            "pid=1 start=current protocol=2 package=0.2.0 build=python \
             writer={build} group=1 token={token} time=1.000\n"
        ),
    )
    .unwrap();
    let adopted = run(&home, &authorized, build, Some(token), false);
    assert!(
        adopted.status.success(),
        "{}",
        String::from_utf8_lossy(&adopted.stderr)
    );
    let owner: serde_json::Value =
        serde_json::from_slice(&fs::read(authorized.join(".derived-owner.json")).unwrap()).unwrap();
    assert_eq!(owner["build_id"], build);
    assert!(
        !authorized.join(".indexd.lock").exists(),
        "legacy adoption claim leaked after publication"
    );

    // Once the durable owner exists, an older daemon can still restart because
    // it does not understand that new anchor. Every current write must keep
    // honoring the legacy daemon namespace, not only the first adoption.
    fs::remove_file(authorized.join(".indexd.v2.lock")).unwrap();
    let post_adoption_daemon = authorized.join(".indexd.v2.lock");
    fs::write(
        &post_adoption_daemon,
        "pid=1 start=old protocol=2 package=0.2.0 build=old \
         writer=aaaaaaaaaaaaaaaaaaaa group=1 \
         token=dddddddddddddddddddddddddddddddd time=2.000\n",
    )
    .unwrap();
    let post_adoption_staging = authorized.join("messages.jsonl.tmp.999999.2");
    fs::write(&post_adoption_staging, b"must still not be swept").unwrap();
    let cache_before = fs::read(authorized.join(".ingest_cache.bin")).unwrap();
    let blocked_again = run(&home, &authorized, build, None, false);
    let blocked_again_stderr = String::from_utf8_lossy(&blocked_again.stderr);
    assert!(blocked_again.status.success(), "{blocked_again_stderr}");
    assert!(
        blocked_again_stderr.contains("belongs to a different writing build"),
        "{blocked_again_stderr}"
    );
    assert!(
        blocked_again_stderr.contains("serving the published snapshot read-only"),
        "{blocked_again_stderr}"
    );
    assert_eq!(
        fs::read(authorized.join(".ingest_cache.bin")).unwrap(),
        cache_before
    );
    assert_eq!(
        fs::read(&post_adoption_staging).unwrap(),
        b"must still not be swept"
    );
    assert!(!authorized.join(".index.lock").exists());
    fs::remove_file(post_adoption_daemon).unwrap();
    fs::remove_file(post_adoption_staging).unwrap();
    let same_build_retry = run(&home, &authorized, build, None, false);
    assert!(
        same_build_retry.status.success(),
        "{}",
        String::from_utf8_lossy(&same_build_retry.stderr)
    );
    assert!(
        !authorized.join(".indexd.lock").exists(),
        "same-build write claim leaked after publication"
    );
    assert!(
        !authorized.join(".indexd.v2.lock").exists(),
        "same-build write claim leaked after publication"
    );

    let identity_fenced = temp_dir("identity-fenced-adoption-data");
    let identity = run(&home, &identity_fenced, build, None, true);
    let identity_stderr = String::from_utf8_lossy(&identity.stderr);
    assert!(identity.status.success(), "{identity_stderr}");
    assert!(
        identity_stderr.contains("derived writer identity is unavailable"),
        "{identity_stderr}"
    );
    assert!(!identity_fenced.join(".index.lock").exists());
    assert!(!identity_fenced.join(".derived-owner.json").exists());
    assert!(!identity_fenced.join(".ingest_cache.bin").exists());

    let _ = fs::remove_dir_all(blocked);
    let _ = fs::remove_dir_all(authorized);
    let _ = fs::remove_dir_all(identity_fenced);
    let _ = fs::remove_dir_all(home);
}

fn install_cli_owned_cold_cache(data: &Path, agent: &str) {
    let empty_home = temp_dir("cli-owned-cold-cache-home");
    let scratch = temp_dir("cli-owned-cold-cache-data");
    ingest_into(agent, &empty_home, &scratch, false);
    fs::copy(
        scratch.join(".ingest_cache.bin"),
        data.join(".ingest_cache.bin"),
    )
    .unwrap();
    let _ = fs::remove_dir_all(empty_home);
    let _ = fs::remove_dir_all(scratch);
}

/// A published source can legitimately identify an adapter that emitted no rows. A valid
/// legacy/upgrade cache has no per-adapter empty snapshot, so event repair must accept a later
/// complete ENOENT preflight instead of inventing an incomplete-read health failure.
#[test]
fn missing_never_materialized_store_stays_a_normal_absence_during_event_repair() {
    let home = temp_dir("missing-empty-store-repair-home");
    let cline = home.join(".cline");
    let state = cline.join("data/state");
    fs::create_dir_all(&state).unwrap();
    fs::write(state.join("taskHistory.json"), b"[]").unwrap();
    let data = temp_dir("missing-empty-store-repair-data");

    ingest_into("cline", &home, &data, false);
    assert!(
        fs::read_to_string(data.join("messages.jsonl"))
            .unwrap()
            .is_empty(),
        "empty Cline history unexpectedly produced indexed rows"
    );
    assert!(!data.join(".source-health.json").exists());
    assert!(data.join(".events_complete.cline.json").exists());

    fs::remove_dir_all(&cline).unwrap();
    install_cli_owned_cold_cache(&data, "cline");
    fs::remove_file(data.join(".events_complete.cline.json")).unwrap();

    let repaired = ingest_output("cline", &home, &data, false);
    assert!(
        repaired.status.success(),
        "{}",
        String::from_utf8_lossy(&repaired.stderr)
    );
    assert!(
        fs::read_to_string(data.join("messages.jsonl"))
            .unwrap()
            .is_empty(),
        "repair invented rows for a missing never-materialized store"
    );
    assert!(!data.join(".source-health.json").exists());
    assert!(data.join(".events_complete.cline.json").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Completeness is per adapter: one chmod-unreadable store in an `all` preflight must not make
/// a different, cleanly absent adapter look like an incomplete read.
#[cfg(unix)]
#[test]
fn mixed_unreadable_and_absent_stores_keep_distinct_health() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("mixed-unreadable-absent-home");
    copy_dir(&fixture_home("claude"), &home);
    let cline_state = home.join(".cline/data/state");
    fs::create_dir_all(&cline_state).unwrap();
    fs::write(cline_state.join("taskHistory.json"), b"[]").unwrap();
    let data = temp_dir("mixed-unreadable-absent-data");

    ingest_into("all", &home, &data, false);
    assert!(data.join(".events_complete.cline.json").exists());
    fs::remove_dir_all(home.join(".cline")).unwrap();
    install_cli_owned_cold_cache(&data, "all");
    fs::remove_file(data.join(".events_complete.cline.json")).unwrap();

    let claude_store = home.join(".claude/projects");
    let permissions = fs::metadata(&claude_store).unwrap().permissions();
    fs::set_permissions(&claude_store, fs::Permissions::from_mode(0o000)).unwrap();
    let degraded = ingest_output("all", &home, &data, false);
    fs::set_permissions(&claude_store, permissions).unwrap();
    assert!(
        !degraded.status.success(),
        "event repair unexpectedly published through the unreadable Claude store"
    );
    let error = String::from_utf8_lossy(&degraded.stderr);
    assert!(error.contains("agent claude"), "{error}");
    assert!(!error.contains("agent cline"), "{error}");

    let health: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    let issues = health["issues"].as_array().unwrap();
    assert!(
        issues
            .iter()
            .any(|issue| issue["agent"] == "claude" && issue["kind"] == "permission-denied"),
        "{health}"
    );
    assert!(
        issues.iter().all(|issue| issue["agent"] != "cline"),
        "cleanly absent Cline acquired fabricated health: {health}"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn legacy_event_generation_requires_complete_all_agent_migration() {
    let home = temp_dir("event-name-migration-home");
    copy_dir(&fixture_home("gemini"), &home);
    let data = temp_dir("event-name-migration-data");
    ingest_into("gemini", &home, &data, false);

    let session = "55555555-5555-4555-8555-555555555555";
    let current = agrep_core::cache::event_fname("gemini", session);
    let legacy = format!("gemini-{session}.jsonl");
    let events = data.join("events");
    let expected = event_row(&data, &current).unwrap();
    fs::remove_file(events.join(agrep_core::cache::EVENT_STORE_NAME)).unwrap();
    let _ = fs::remove_file(events.join(format!("{}-wal", agrep_core::cache::EVENT_STORE_NAME)));
    let _ = fs::remove_file(events.join(format!("{}-shm", agrep_core::cache::EVENT_STORE_NAME)));
    fs::write(events.join(&legacy), &expected).unwrap();
    let scoped = ingest_output("gemini", &home, &data, false);
    assert!(!scoped.status.success());
    assert!(String::from_utf8_lossy(&scoped.stderr).contains("without `--agent`"));
    assert!(!events.join(agrep_core::cache::EVENT_STORE_NAME).exists());
    assert_eq!(fs::read(events.join(&legacy)).unwrap(), expected);

    ingest_into("all", &home, &data, true);
    assert_eq!(event_row(&data, &current).unwrap(), expected);
    assert!(events.join(&legacy).exists());
    assert!(!events.join(".manifest").exists());
    assert!(!events.join(".stats").exists());
    let warm = ingest_output("all", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("skipped ingest + writes"));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A missing authoritative DB row must force source reconstruction despite a valid source snapshot.
#[test]
fn source_identical_run_repairs_missing_event_artifacts() {
    let home = temp_dir("event-repair-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("event-repair-data");
    ingest_into("claude", &home, &data, false);

    let events_dir = data.join("events");
    let (event_name, expected) = event_rows(&data).into_iter().next().unwrap();
    let expected_messages = fs::read(data.join("messages.jsonl")).unwrap();
    let expected_replies = fs::read(data.join("replies.jsonl")).unwrap();
    let expected_sessions = fs::read(data.join("sessions.jsonl")).unwrap();
    assert!(data.join(".events_complete.claude.json").exists());

    delete_event_row(&data, &event_name);
    ingest_into("claude", &home, &data, false);
    assert_eq!(event_row(&data, &event_name).unwrap(), expected);
    assert!(data.join(".source_snapshot.bin").exists());

    assert!(!events_dir.join(".manifest").exists());
    assert!(data.join(".events_complete.claude.json").exists());
    assert!(data.join(".source_snapshot.bin").exists());

    // Cache/sig/snapshot still prove a generation existed even with every materialized
    // JSONL gone, so unchanged sources must reconstruct the whole set.
    fs::remove_file(data.join("messages.jsonl")).unwrap();
    fs::remove_file(data.join("replies.jsonl")).unwrap();
    fs::remove_file(data.join("sessions.jsonl")).unwrap();
    fs::remove_file(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap();
    delete_event_row(&data, &event_name);
    fs::remove_file(data.join(".events_complete.claude.json")).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(
        fs::read(data.join("messages.jsonl")).unwrap(),
        expected_messages
    );
    assert_eq!(
        fs::read(data.join("replies.jsonl")).unwrap(),
        expected_replies
    );
    assert_eq!(
        fs::read(data.join("sessions.jsonl")).unwrap(),
        expected_sessions
    );
    assert_eq!(event_row(&data, &event_name).unwrap(), expected);
    assert!(!events_dir.join(".manifest").exists());
    assert!(data.join(".events_complete.claude.json").exists());
    assert!(data.join(".source_snapshot.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A real source deletion can coincide with loss of the event-completeness proof. Repair must
/// reconstruct every surviving event from source, accept the individual deletion only after the
/// stable whole-source pre/post proof, and prune the deleted source's exact event ownership.
#[test]
fn event_repair_converges_a_stable_source_and_event_deletion() {
    let home = temp_dir("event-repair-delete-home");
    copy_dir(&fixture_home("claude"), &home);
    let deleted_source = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001")
        .join("subagents")
        .join("agent-atestchild01.jsonl");
    let data = temp_dir("event-repair-delete-data");

    ingest_into("claude", &home, &data, false);
    let events_dir = data.join("events");
    let deleted_name = agrep_core::cache::event_fname("claude", "agent-atestchild01");
    let surviving_name =
        agrep_core::cache::event_fname("claude", "11111111-1111-4111-8111-111111111111");
    let surviving_body = event_row(&data, &surviving_name).unwrap();
    assert!(
        event_row(&data, &deleted_name).is_some(),
        "fixture must publish child-owned events"
    );
    assert!(data.join(".events_complete.claude.json").exists());

    // Losing a canary forces full reconstruction while source ownership changes.
    fs::remove_file(data.join(".events_complete.claude.json")).unwrap();
    delete_event_row(&data, &surviving_name);
    fs::remove_file(&deleted_source).unwrap();
    ingest_into("claude", &home, &data, false);

    let repaired = normalize(&data);
    assert!(!repaired.contains("agent-atestchild01"));
    assert!(!repaired.contains("toolu_child01"));
    assert!(
        event_row(&data, &deleted_name).is_none(),
        "stale child event row survived repair"
    );
    assert_eq!(event_row(&data, &surviving_name).unwrap(), surviving_body);
    assert!(!events_dir.join(".manifest").exists());
    let event_stats = fs::read_to_string(data.join("event_stats.json")).unwrap();
    assert!(
        !event_stats.contains("Grep"),
        "deleted event survived in rollup"
    );
    assert!(data.join(".events_complete.claude.json").exists());
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());

    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("unchanged since last index"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The parse cache is agrep's own rebuildable artifact: a payload this build cannot decode is
/// discarded and reparsed in the same pass, with the discard disclosed. No flag, no second run.
#[test]
fn undecodable_cache_event_repair_publishes_the_changed_source_in_one_pass() {
    let home = temp_dir("event-repair-cold-change-home");
    copy_dir(&fixture_home("claude"), &home);
    let source = claude_source(&home);
    let data = temp_dir("event-repair-cold-change-data");
    ingest_into("claude", &home, &data, false);

    force_ambiguous_event_recovery(&data);
    let body = fs::read_to_string(&source)
        .unwrap()
        .replace("flaky timer test", "flaky timer zest");
    fs::write(&source, body).unwrap();
    let first = ingest_output("claude", &home, &data, false);
    assert!(
        first.status.success(),
        "an undecodable cache still wedged recovery: {}",
        String::from_utf8_lossy(&first.stderr)
    );
    assert!(
        String::from_utf8_lossy(&first.stdout).contains("discarded an undecodable parse cache"),
        "the discard was not disclosed: {}",
        String::from_utf8_lossy(&first.stdout)
    );

    let repaired = normalize(&data);
    assert!(repaired.contains("flaky timer zest"));
    assert!(!repaired.contains("flaky timer test"));
    assert_ne!(
        fs::read(data.join(".ingest_cache.bin")).unwrap(),
        b"deliberately corrupt cache"
    );
    assert!(data.join(".events_complete.claude.json").exists());
    assert!(!data.join(".ingest_pending.bin").exists());
    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("skipped ingest + writes"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Discarding an undecodable cache must not buy a deletion a cheaper proof: with no last-good
/// rows behind it, a stable deletion still converges only after the second observation.
#[test]
fn undecodable_cache_event_repair_requires_two_observations_for_deletion() {
    let home = temp_dir("event-repair-cold-delete-home");
    copy_dir(&fixture_home("claude"), &home);
    let deleted_source = home
        .join(".claude/projects/proj-alpha/sess-claude-0001/subagents")
        .join("agent-atestchild01.jsonl");
    let data = temp_dir("event-repair-cold-delete-data");
    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);
    let deleted_event = agrep_core::cache::event_fname("claude", "agent-atestchild01");
    assert!(event_row(&data, &deleted_event).is_some());

    force_ambiguous_event_recovery(&data);
    fs::remove_file(&deleted_source).unwrap();
    let first = ingest_output("claude", &home, &data, false);
    assert_retained_generation_refusal(&first);
    assert_eq!(normalize(&data), published);
    assert!(event_row(&data, &deleted_event).is_some());

    ingest_into("claude", &home, &data, false);
    let repaired = normalize(&data);
    assert!(!repaired.contains("agent-atestchild01"));
    assert!(!repaired.contains("toolu_child01"));
    assert!(event_row(&data, &deleted_event).is_none());
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A moving source restarts the observation window instead of inheriting permission from an
/// earlier pending preflight.
#[test]
fn missing_cache_event_repair_rotates_drifting_preflight() {
    let home = temp_dir("event-repair-cold-drift-home");
    copy_dir(&fixture_home("claude"), &home);
    let source = claude_source(&home);
    let data = temp_dir("event-repair-cold-drift-data");
    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);
    force_cacheless_event_recovery(&data);

    let body = fs::read_to_string(&source)
        .unwrap()
        .replace("flaky timer test", "flaky timer zest");
    fs::write(&source, body).unwrap();
    let first = ingest_output("claude", &home, &data, false);
    assert_stable_retry_refusal(&first);
    let first_pending = fs::read(data.join(".ingest_pending.bin")).unwrap();

    let body = fs::read_to_string(&source)
        .unwrap()
        .replace("flaky timer zest", "flaky timer best");
    fs::write(&source, body).unwrap();
    let second = ingest_output("claude", &home, &data, false);
    assert_stable_retry_refusal(&second);
    let second_pending = fs::read(data.join(".ingest_pending.bin")).unwrap();
    assert_ne!(first_pending, second_pending);
    assert_eq!(normalize(&data), published);

    ingest_into("claude", &home, &data, false);
    let repaired = normalize(&data);
    assert!(repaired.contains("flaky timer best"));
    assert!(!repaired.contains("flaky timer zest"));
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// An old snapshot-version marker must begin a fresh observation window, then let an ordinary
/// retry migrate the generation without `--full`.
#[test]
fn missing_cache_event_repair_rotates_stale_snapshot_version() {
    let home = temp_dir("event-repair-stale-pending-home");
    copy_dir(&fixture_home("claude"), &home);
    let source = claude_source(&home);
    let data = temp_dir("event-repair-stale-pending-data");
    ingest_into("claude", &home, &data, false);
    force_cacheless_event_recovery(&data);

    let body = fs::read_to_string(&source)
        .unwrap()
        .replace("flaky timer test", "flaky timer zest");
    fs::write(&source, body).unwrap();
    let mut stale = fs::read(data.join(".source_snapshot.bin")).unwrap();
    stale[..4].copy_from_slice(&8u32.to_le_bytes());
    fs::write(data.join(".ingest_pending.bin"), &stale).unwrap();

    let first = ingest_output("claude", &home, &data, false);
    assert_stable_retry_refusal(&first);
    assert_ne!(fs::read(data.join(".ingest_pending.bin")).unwrap(), stale);
    ingest_into("claude", &home, &data, false);
    assert!(normalize(&data).contains("flaky timer zest"));
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Discarding an undecodable cache is not permission to publish past a denied scope: an
/// incomplete preflight still retains the old generation and its complete pending expectation.
#[cfg(unix)]
#[test]
fn undecodable_cache_event_repair_preserves_pending_on_incomplete_preflight() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("event-repair-incomplete-home");
    copy_dir(&fixture_home("claude"), &home);
    let project = home.join(".claude/projects/proj-alpha");
    let data = temp_dir("event-repair-incomplete-data");
    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);
    let pending = fs::read(data.join(".source_snapshot.bin")).unwrap();
    fs::write(data.join(".ingest_pending.bin"), &pending).unwrap();
    force_ambiguous_event_recovery(&data);

    let permissions = fs::metadata(&project).unwrap().permissions();
    fs::set_permissions(&project, fs::Permissions::from_mode(0o000)).unwrap();
    let failed = ingest_output("claude", &home, &data, false);
    fs::set_permissions(&project, permissions).unwrap();
    assert_retained_generation_refusal(&failed);
    assert_eq!(fs::read(data.join(".ingest_pending.bin")).unwrap(), pending);
    assert_eq!(normalize(&data), published);

    ingest_into("claude", &home, &data, false);
    assert_eq!(normalize(&data), published);
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn unreadable_known_store_preserves_generation_and_stays_degraded() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("unreadable-known-store-home");
    copy_dir(&fixture_home("claude"), &home);
    let store = home.join(".claude/projects");
    let data = temp_dir("unreadable-known-store-data");
    ingest_into("claude", &home, &data, true);
    let published = normalize(&data);
    let signature = data.join(".ingest.sig");
    let signature_body = fs::read(&signature).unwrap();
    let signature_time = fs::metadata(&signature).unwrap().modified().unwrap();

    let permissions = fs::metadata(&store).unwrap().permissions();
    fs::set_permissions(&store, fs::Permissions::from_mode(0o000)).unwrap();
    let first = ingest_output("claude", &home, &data, false);
    let second = ingest_output("claude", &home, &data, false);
    fs::set_permissions(&store, permissions).unwrap();

    let error = String::from_utf8_lossy(&first.stderr);
    assert!(first.status.success(), "{error}");
    assert!(error.contains(store.to_string_lossy().as_ref()), "{error}");
    // The stable denial converges: the second run reaches the all-hit shortcut
    // instead of a permanent pending retry, and the freshness clock keeps beating.
    assert!(second.status.success());
    assert!(String::from_utf8_lossy(&second.stdout).contains("unchanged since last index"));
    assert_eq!(normalize(&data), published);
    assert_eq!(fs::read(&signature).unwrap(), signature_body);
    assert!(fs::metadata(&signature).unwrap().modified().unwrap() >= signature_time);
    assert!(!data.join(".ingest_pending.bin").exists());
    let health: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    assert_eq!(health["code"], "source-unreadable");
    assert_eq!(
        health["issues"][0]["path"],
        store.to_string_lossy().as_ref()
    );

    ingest_into("claude", &home, &data, false);
    assert!(!data.join(".source-health.json").exists());
    assert_eq!(normalize(&data), published);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn unreadable_file_reports_exact_path_in_health_and_stores() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;

    let home = temp_dir("unreadable-file-home");
    copy_dir(&fixture_home("claude"), &home);
    let source = home
        .join(".claude/projects/proj-alpha")
        .join("sess-claude-0001.jsonl");
    let data = temp_dir("unreadable-file-data");
    ingest_into("claude", &home, &data, true);

    let permissions = fs::metadata(&source).unwrap().permissions();
    fs::set_permissions(&source, fs::Permissions::from_mode(0o000)).unwrap();
    let degraded = ingest_output("claude", &home, &data, false);
    fs::set_permissions(&source, permissions).unwrap();
    assert!(
        degraded.status.success(),
        "{}",
        String::from_utf8_lossy(&degraded.stderr)
    );

    let health: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    assert_eq!(health["issues"][0]["agent"], "claude");
    assert_eq!(
        health["issues"][0]["path"],
        source.to_string_lossy().as_ref()
    );

    let mut command = Command::new(BIN);
    command.arg("stores");
    command.env("AGREP_HOME", &home);
    command.env("AGREP_DATA_DIR", &data);
    for key in [
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "CLINE_DIR",
        "XDG_CONFIG_HOME",
        "AGREP_RS_BIN",
    ] {
        command.env_remove(key);
    }
    let stores = command.output().unwrap();
    assert!(
        stores.status.success(),
        "{}",
        String::from_utf8_lossy(&stores.stderr)
    );
    let rows: serde_json::Value = serde_json::from_slice(&stores.stdout).unwrap();
    let claude = rows
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["name"] == "claude")
        .unwrap();
    assert_eq!(claude["state"], "source-unreadable");
    assert_eq!(
        claude["issues"][0]["path"],
        source.to_string_lossy().as_ref()
    );

    ingest_into("claude", &home, &data, false);
    assert!(!data.join(".source-health.json").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Automatic event reconstruction must be transactional when a source becomes unreadable after
/// preflight. The last-good cache may serve rows for this process, but no partial event proof,
/// cache generation, derived output, or source permission slip may be published.
#[test]
fn event_repair_aborts_on_transient_empty_source_then_retries() {
    let home = temp_dir("guarded-event-repair-home");
    copy_dir(&fixture_home("claude"), &home);
    let source = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    let source_body = fs::read(&source).unwrap();
    let data = temp_dir("guarded-event-repair-data");
    ingest_into("claude", &home, &data, false);

    let (event_name, event_body) = event_rows(&data).into_iter().next().unwrap();
    let cache_before = fs::read(data.join(".ingest_cache.bin")).unwrap();
    let sig_before = fs::read(data.join(".ingest.sig")).unwrap();
    let messages_before = fs::read(data.join("messages.jsonl")).unwrap();
    let replies_before = fs::read(data.join("replies.jsonl")).unwrap();
    let sessions_before = fs::read(data.join("sessions.jsonl")).unwrap();
    let families_before = fs::read(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap();

    delete_event_row(&data, &event_name);
    fs::remove_file(data.join(".events_complete.claude.json")).unwrap();
    fs::write(&source, b"").unwrap();
    let failed = ingest_output("claude", &home, &data, false);
    assert!(
        !failed.status.success(),
        "guarded repair unexpectedly succeeded"
    );
    assert!(
        String::from_utf8_lossy(&failed.stderr).contains("event repair observed"),
        "unexpected failure: {}",
        String::from_utf8_lossy(&failed.stderr)
    );
    assert_eq!(
        fs::read(data.join(".ingest_cache.bin")).unwrap(),
        cache_before
    );
    assert_eq!(fs::read(data.join(".ingest.sig")).unwrap(), sig_before);
    assert_eq!(
        fs::read(data.join("messages.jsonl")).unwrap(),
        messages_before
    );
    assert_eq!(
        fs::read(data.join("replies.jsonl")).unwrap(),
        replies_before
    );
    assert_eq!(
        fs::read(data.join("sessions.jsonl")).unwrap(),
        sessions_before
    );
    assert_eq!(
        fs::read(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap(),
        families_before
    );
    assert!(
        data.join(".source_snapshot.bin").exists(),
        "failed repair must preserve the last published snapshot"
    );
    assert!(
        data.join(".ingest_pending.bin").exists(),
        "failed repair must leave a durable retry preflight"
    );
    assert!(!data.join(".events_complete.claude.json").exists());
    assert!(event_row(&data, &event_name).is_none());

    fs::write(&source, source_body).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(event_row(&data, &event_name).unwrap(), event_body);
    assert!(data.join(".events_complete.claude.json").exists());
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(not(windows))]
#[test]
fn rejected_message_publication_cannot_commit_event_generation() {
    let home = temp_dir("atomic-derived-home");
    copy_dir(&fixture_home("claude"), &home);
    let source = home.join(".claude/projects/proj-alpha/sess-claude-0001.jsonl");
    let original_source = fs::read(&source).unwrap();
    let data = temp_dir("atomic-derived-data");
    ingest_into("claude", &home, &data, false);

    let event_name =
        agrep_core::cache::event_fname("claude", "11111111-1111-4111-8111-111111111111");
    let event_before = event_row(&data, &event_name).unwrap();
    let messages_before = fs::read(data.join("messages.jsonl")).unwrap();
    let marker = "atomic publication event marker 7e41";
    let mut changed_source = original_source.clone();
    changed_source.extend_from_slice(
        format!(
            concat!(
                "{{\"type\":\"user\",\"userType\":\"external\",",
                "\"sessionId\":\"11111111-1111-4111-8111-111111111111\",",
                "\"timestamp\":\"2026-01-02T13:00:00.000Z\",\"cwd\":\"/work/alpha\",",
                "\"message\":{{\"role\":\"user\",\"content\":\"{}\"}}}}\n",
                "{{\"type\":\"assistant\",",
                "\"sessionId\":\"11111111-1111-4111-8111-111111111111\",",
                "\"timestamp\":\"2026-01-02T13:00:01.000Z\",\"cwd\":\"/work/alpha\",",
                "\"message\":{{\"role\":\"assistant\",\"model\":\"claude-fable-5\",",
                "\"content\":[{{\"type\":\"tool_use\",\"id\":\"toolu_atomic_7e41\",",
                "\"name\":\"Bash\",\"input\":{{\"command\":\"echo {}\"}}}}]}}}}\n"
            ),
            marker, marker,
        )
        .as_bytes(),
    );
    fs::write(&source, &changed_source).unwrap();

    let messages = data.join("messages.jsonl");
    let parked = data.join("messages.before-atomic-failure");
    fs::rename(&messages, &parked).unwrap();
    fs::create_dir(&messages).unwrap();
    let failed = ingest_output("claude", &home, &data, false);
    assert!(
        !failed.status.success(),
        "output sabotage unexpectedly succeeded"
    );
    assert_eq!(event_row(&data, &event_name).unwrap(), event_before);
    assert!(!data.join(".events_complete.claude.json").exists());

    fs::remove_dir(&messages).unwrap();
    fs::rename(&parked, &messages).unwrap();
    fs::write(&source, b"").unwrap();
    let unavailable = ingest_output("claude", &home, &data, false);
    assert!(!unavailable.status.success());
    assert_eq!(fs::read(&messages).unwrap(), messages_before);
    assert_eq!(event_row(&data, &event_name).unwrap(), event_before);

    fs::write(&source, changed_source).unwrap();
    ingest_into("claude", &home, &data, false);
    assert!(normalize(&data).contains(marker));
    assert!(data.join(".events_complete.claude.json").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A combined claude+antigravity home with a claude session that keeps growing between runs -
/// the live-box shape a whole-store deletion has to converge under.
fn churning_two_store_home() -> (PathBuf, PathBuf, PathBuf) {
    let home = temp_dir("two-store-home");
    copy_dir(&fixture_home("claude"), &home);
    copy_dir(&fixture_home("antigravity"), &home);
    let churn = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    (
        home.clone(),
        home.join(".gemini/antigravity-cli/brain"),
        churn,
    )
}

fn append_claude_churn(source: &Path, minute: u32) {
    let mut body = fs::read_to_string(source).unwrap();
    body.push_str(&format!(
        concat!(
            "{{\"type\":\"user\",\"userType\":\"external\",",
            "\"sessionId\":\"11111111-1111-4111-8111-111111111111\",",
            "\"timestamp\":\"2026-01-02T10:{:02}:00.000Z\",\"cwd\":\"/work/alpha\",",
            "\"message\":{{\"role\":\"user\",\"content\":\"churn probe {}\"}}}}\n"
        ),
        minute, minute
    ));
    fs::write(source, body).unwrap();
}

fn has_antigravity_rows(data: &Path) -> bool {
    fs::read_to_string(data.join("messages.jsonl"))
        .unwrap()
        .contains("\"agent\":\"antigravity\"")
}

/// An uninstalled store is complete knowledge: ENOENT at the root, observed by two complete
/// preflights, must converge even while unrelated sources keep changing between runs. The
/// first repair run retains and records the absence; the second confirms the tombstone,
/// publishes, and leaves no stale-source hedge for queries to render.
#[test]
fn vanished_root_converges_despite_unrelated_source_churn() {
    let (home, brain, churn) = churning_two_store_home();
    let data = temp_dir("vanished-root-data");
    ingest_into("all", &home, &data, true);
    assert!(has_antigravity_rows(&data));

    fs::remove_dir_all(&brain).unwrap();
    fs::remove_file(data.join(".events_complete.antigravity.json")).unwrap();
    append_claude_churn(&churn, 4);
    let first = ingest_output("all", &home, &data, false);
    assert!(
        !first.status.success(),
        "first absence must retain and retry"
    );
    assert!(
        String::from_utf8_lossy(&first.stderr).contains("event repair observed"),
        "unexpected failure: {}",
        String::from_utf8_lossy(&first.stderr)
    );
    assert!(has_antigravity_rows(&data));
    assert!(data.join(".source-health.json").exists());

    append_claude_churn(&churn, 5);
    let second = ingest_output("all", &home, &data, false);
    assert!(
        second.status.success(),
        "repeated clean absence must converge:\n{}",
        String::from_utf8_lossy(&second.stderr)
    );
    assert!(!has_antigravity_rows(&data));
    assert!(!data.join(".source-health.json").exists());
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(data.join(".events_complete.antigravity.json").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn unrelated_unreadable_adapter_does_not_veto_root_deletion() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("cross-adapter-delete-home");
    copy_dir(&fixture_home("claude"), &home);
    copy_dir(&fixture_home("cline"), &home);
    let claude_root = home.join(".claude/projects");
    let cline_source = home.join(".cline/data/tasks/1767348000000/api_conversation_history.json");
    let data = temp_dir("cross-adapter-delete-data");
    ingest_into("all", &home, &data, false);
    assert!(normalize(&data).contains("\"agent\":\"claude\""));

    fs::set_permissions(&cline_source, fs::Permissions::from_mode(0o000)).unwrap();
    ingest_into("all", &home, &data, false);
    assert!(normalize(&data).contains("\"agent\":\"claude\""));

    fs::remove_dir_all(&claude_root).unwrap();
    ingest_into("all", &home, &data, false);
    assert!(normalize(&data).contains("\"agent\":\"claude\""));

    ingest_into("all", &home, &data, false);
    let published = normalize(&data);
    assert!(!published.contains("\"agent\":\"claude\""));
    assert!(published.contains("build a cli flag parser"));
    // The confirmed tombstone publishes beside the stably-denied sibling, so the
    // pending marker retires in the same run instead of awaiting the denied read.
    assert!(!data.join(".ingest_pending.bin").exists());

    fs::set_permissions(&cline_source, fs::Permissions::from_mode(0o600)).unwrap();
    ingest_into("all", &home, &data, false);
    assert!(!normalize(&data).contains("\"agent\":\"claude\""));
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(!data.join(".source_absence_pending").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A root that exists but cannot be read is a durable disclosed fact: repair keeps retaining
/// last-good rows run after run - never tombstoning through the guard - publishes the
/// readable stores, and keeps the source-health record until the root is readable again.
#[cfg(unix)]
#[test]
fn permission_denied_root_retains_rows_across_repair_retries() {
    use std::os::unix::fs::PermissionsExt;

    let (home, brain, churn) = churning_two_store_home();
    let store = brain.parent().unwrap().to_path_buf();
    let data = temp_dir("denied-root-data");
    ingest_into("all", &home, &data, true);
    assert!(has_antigravity_rows(&data));

    fs::remove_file(data.join(".events_complete.antigravity.json")).unwrap();
    fs::set_permissions(&store, fs::Permissions::from_mode(0o000)).unwrap();
    for minute in [4, 5] {
        append_claude_churn(&churn, minute);
        let denied = ingest_output("all", &home, &data, false);
        assert!(
            denied.status.success(),
            "denied root must not kill publication: {}",
            String::from_utf8_lossy(&denied.stderr)
        );
        assert!(has_antigravity_rows(&data));
        assert!(
            fs::read_to_string(data.join("messages.jsonl"))
                .unwrap()
                .contains(&format!("churn probe {minute}")),
            "readable churn was not published under the denied sibling root"
        );
        assert!(data.join(".source-health.json").exists());
    }
    fs::set_permissions(&store, fs::Permissions::from_mode(0o755)).unwrap();

    ingest_into("all", &home, &data, false);
    assert!(has_antigravity_rows(&data));
    assert!(!data.join(".source-health.json").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The steady-state shortcut happens before parse-cache deserialization/save. A corrupt cache
/// therefore cannot slow an unchanged published index, but the first real source change must
/// fall through and rebuild that cache rather than hiding behind the source snapshot.
#[test]
fn source_snapshot_shortcuts_before_cache_load_then_repairs_on_change() {
    let home = temp_dir("source-shortcut-home");
    copy_dir(&fixture_home("claude"), &home);
    let file = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    let data = temp_dir("source-shortcut-data");

    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);
    assert!(data.join(".source_snapshot.bin").exists());
    let cache_path = data.join(".ingest_cache.bin");
    let corrupt_cache = corrupt_current_cache_payload(&data);

    ingest_into("claude", &home, &data, false);
    assert_eq!(published, normalize(&data));
    assert_eq!(fs::read(&cache_path).unwrap(), corrupt_cache);

    let mut body = fs::read_to_string(&file).unwrap();
    body.push_str(concat!(
        "\n{\"type\":\"user\",\"userType\":\"external\",",
        "\"sessionId\":\"11111111-1111-4111-8111-111111111111\",",
        "\"timestamp\":\"2026-01-02T10:03:00.000Z\",\"cwd\":\"/work/alpha\",",
        "\"message\":{\"role\":\"user\",\"content\":\"source snapshot repair probe\"}}\n"
    ));
    fs::write(&file, body).unwrap();
    ingest_into("claude", &home, &data, false);
    assert!(normalize(&data).contains("source snapshot repair probe"));
    assert_ne!(fs::read(&cache_path).unwrap(), corrupt_cache);

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Corpus-local harness classification is a materialized-output input even when every
/// transcript file is unchanged. Its exact bytes must invalidate the early all-hit shortcut,
/// and an unreadable/invalid policy must retain the last good published generation.
#[test]
fn harness_policy_change_invalidates_source_shortcut() {
    let home = temp_dir("harness-policy-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("harness-policy-data");
    let policy = data.join("harness_prefixes.txt");
    let target = "how do i fix the flaky timer test";
    let row_who = |data: &Path, target: &str| -> String {
        fs::read_to_string(data.join("messages.jsonl"))
            .unwrap()
            .lines()
            .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
            .find(|row| row.get("text").and_then(|value| value.as_str()) == Some(target))
            .and_then(|row| {
                row.get("who")
                    .and_then(|value| value.as_str())
                    .map(str::to_string)
            })
            .expect("target fixture row")
    };

    ingest_into("claude", &home, &data, false);
    assert_eq!(row_who(&data, target), "user");
    assert!(data.join(".harness_prefixes.snapshot").exists());

    fs::write(&policy, "how do i fix\n").unwrap();
    let changed = ingest_output("claude", &home, &data, false);
    assert!(
        changed.status.success(),
        "{}",
        String::from_utf8_lossy(&changed.stderr)
    );
    assert!(!String::from_utf8_lossy(&changed.stdout).contains("skipped ingest + writes"));
    assert_eq!(row_who(&data, target), "harness");
    let last_good = normalize(&data);

    fs::write(&policy, [0xff]).unwrap();
    let invalid = ingest_output("claude", &home, &data, false);
    assert!(
        !invalid.status.success(),
        "invalid UTF-8 policy must be refused"
    );
    assert_eq!(normalize(&data), last_good);

    fs::remove_file(&policy).unwrap();
    let removed = ingest_output("claude", &home, &data, false);
    assert!(
        removed.status.success(),
        "{}",
        String::from_utf8_lossy(&removed.stderr)
    );
    assert_eq!(row_who(&data, target), "user");
    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("skipped ingest + writes"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A crash can replace one derived file while leaving the old signature and cache intact.
/// The pending marker must suppress both the early source shortcut and the later content-sig
/// shortcut so recovery rewrites the entire derived generation before clearing the marker.
#[test]
fn pending_retry_repairs_a_partially_published_derived_generation() {
    let home = temp_dir("pending-partial-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("pending-partial-data");

    ingest_into("claude", &home, &data, false);
    let expected_messages = fs::read(data.join("messages.jsonl")).unwrap();
    let expected_replies = fs::read(data.join("replies.jsonl")).unwrap();
    let expected_sessions = fs::read(data.join("sessions.jsonl")).unwrap();
    let expected_families =
        fs::read(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap();
    let preflight = fs::read(data.join(".source_snapshot.bin")).unwrap();

    fs::write(data.join("replies.jsonl"), b"partial crash output\n").unwrap();
    fs::write(data.join(".ingest_pending.bin"), preflight).unwrap();
    ingest_into("claude", &home, &data, false);

    assert_eq!(
        fs::read(data.join("messages.jsonl")).unwrap(),
        expected_messages
    );
    assert_eq!(
        fs::read(data.join("replies.jsonl")).unwrap(),
        expected_replies
    );
    assert_eq!(
        fs::read(data.join("sessions.jsonl")).unwrap(),
        expected_sessions
    );
    assert_eq!(
        fs::read(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap(),
        expected_families
    );
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn warm_shortcut_repairs_every_corrupt_derived_output() {
    let home = temp_dir("derived-proof-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("derived-proof-data");
    ingest_into("claude", &home, &data, false);
    let targets = [
        "messages.jsonl",
        "replies.jsonl",
        "sessions.jsonl",
        agrep_core::cache::SESSION_FAMILY_META_FILE,
        agrep_core::boundary_stats::FILE_NAME,
        agrep_core::boundary_stats::CACHE_FILE_NAME,
        "event_stats.json",
    ];
    let expected: Vec<Vec<u8>> = targets
        .iter()
        .map(|name| fs::read(data.join(name)).unwrap())
        .collect();

    for (name, body) in targets.iter().zip(&expected) {
        fs::write(data.join(name), b"").unwrap();
        let output = ingest_output("claude", &home, &data, false);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let repaired = fs::read(data.join(name)).unwrap();
        if *name == agrep_core::boundary_stats::CACHE_FILE_NAME {
            assert!(!repaired.is_empty(), "{name} was not repaired");
        } else {
            assert_eq!(repaired, *body, "{name} was not repaired");
        }
        assert!(!String::from_utf8_lossy(&output.stdout).contains("skipped ingest + writes"));
    }

    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("skipped ingest + writes"));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The streamed first-hit lane deliberately skips the expensive source preflight. It must still
/// commit its healthy parse cache and let the next normal ingest reach a validated generation
/// using cache hits instead of repeating the cold parse. Having recorded no preflight, it
/// leaves no marker: the absent snapshot is already what holds first-use readers closed.
#[test]
fn emit_rows_cold_ingest_hands_useful_cache_to_normal_generation() {
    let home = temp_dir("emit-handoff-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("emit-handoff-data");

    let streamed = ingest_emit_output("claude", &home, &data);
    assert!(
        streamed.status.success(),
        "streamed ingest failed: {}",
        String::from_utf8_lossy(&streamed.stderr)
    );
    assert!(
        String::from_utf8_lossy(&streamed.stdout).contains("\"row\":"),
        "streamed ingest emitted no materialized row"
    );
    assert!(data.join(".ingest_cache.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(!data.join(".source_snapshot.bin").exists());

    let handoff = ingest_output("claude", &home, &data, false);
    assert!(
        handoff.status.success(),
        "normal handoff failed: {}",
        String::from_utf8_lossy(&handoff.stderr)
    );
    let handoff_stdout = String::from_utf8_lossy(&handoff.stdout);
    assert!(
        handoff_stdout.contains("0 changed"),
        "normal handoff repeated the cold parse:\n{handoff_stdout}"
    );
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());

    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(
        String::from_utf8_lossy(&warm.stdout).contains("unchanged since last index"),
        "handoff did not converge to the all-hit shortcut"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Pending recovery may accept an individual source deletion only after the complete source
/// snapshot is unchanged across collection. This exercises the real main-level pre/post proof,
/// including affected-sibling event reconstruction for two files sharing one session.
#[test]
fn pending_retry_converges_a_stable_stat_source_deletion() {
    let home = temp_dir("pending-stat-delete-home");
    copy_dir(&fixture_home("claude"), &home);
    let deleted = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001")
        .join("subagents")
        .join("agent-atestchild01.jsonl");
    let data = temp_dir("pending-stat-delete-data");

    ingest_into("claude", &home, &data, false);
    let before = normalize(&data);
    assert!(before.contains("Explore the auth module"));
    let pending = fs::read(data.join(".source_snapshot.bin")).unwrap();
    fs::write(data.join(".ingest_pending.bin"), pending).unwrap();
    fs::remove_file(&deleted).unwrap();

    ingest_into("claude", &home, &data, false);
    let after = normalize(&data);
    assert!(!after.contains("Explore the auth module"));
    assert!(!after.contains("toolu_child01"));
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());

    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("unchanged since last index"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A live append elsewhere cannot hold an exact file deletion or stable addition hostage.
#[test]
fn pending_retry_publishes_under_a_continuous_unrelated_writer() {
    let home = temp_dir("pending-live-writer-home");
    copy_dir(&fixture_home("claude"), &home);
    let project = home.join(".claude").join("projects").join("proj-alpha");
    let churn = project.join("sess-claude-0001.jsonl");
    let deleted = project
        .join("sess-claude-0001")
        .join("subagents")
        .join("agent-atestchild01.jsonl");
    let data = temp_dir("pending-live-writer-data");

    let seed = fs::read_to_string(&churn).unwrap();
    let first = seed.lines().next().unwrap();
    let mut amplified = String::with_capacity(seed.len() + first.len() * 5_000);
    amplified.push_str(&seed);
    for _ in 0..5_000 {
        amplified.push_str(first);
        amplified.push('\n');
    }
    fs::write(&churn, amplified).unwrap();
    ingest_into("claude", &home, &data, false);
    fs::copy(
        data.join(".source_snapshot.bin"),
        data.join(".ingest_pending.bin"),
    )
    .unwrap();
    fs::remove_file(&deleted).unwrap();

    let marker = "live writer additive marker 91f3";
    fs::write(
        project.join("live-writer-addition.jsonl"),
        format!(
            concat!(
                "{{\"type\":\"user\",\"userType\":\"external\",",
                "\"sessionId\":\"91919191-9191-4191-8191-919191919191\",",
                "\"timestamp\":\"2026-01-02T11:00:00.000Z\",\"cwd\":\"/work/live\",",
                "\"message\":{{\"role\":\"user\",\"content\":\"{}\"}}}}\n"
            ),
            marker
        ),
    )
    .unwrap();

    let running = Arc::new(AtomicBool::new(true));
    let ready = Arc::new(Barrier::new(2));
    let writer_running = Arc::clone(&running);
    let writer_ready = Arc::clone(&ready);
    let writer_source = churn.clone();
    let writer = std::thread::spawn(move || {
        let mut file = OpenOptions::new().append(true).open(writer_source).unwrap();
        writer_ready.wait();
        let mut sequence = 0u64;
        while writer_running.load(Ordering::Acquire) {
            writeln!(
                file,
                concat!(
                    "{{\"type\":\"user\",\"userType\":\"external\",",
                    "\"sessionId\":\"11111111-1111-4111-8111-111111111111\",",
                    "\"timestamp\":\"2026-01-02T12:00:00.000Z\",\"cwd\":\"/work/alpha\",",
                    "\"message\":{{\"role\":\"user\",\"content\":\"live churn {}\"}}}}"
                ),
                sequence
            )
            .unwrap();
            file.flush().unwrap();
            sequence += 1;
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
    });
    ready.wait();
    let output = ingest_output("claude", &home, &data, false);
    running.store(false, Ordering::Release);
    writer.join().unwrap();

    assert!(
        output.status.success(),
        "live writer starved publication: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let published = normalize(&data);
    assert!(published.contains(marker));
    assert!(!published.contains("Explore the auth module"));
    assert!(!published.contains("toolu_child01"));
    // Clean reads under live drift publish the preflight generation and retire
    // pending; a host too slow for a clean read defers a hot tail, which the
    // quiescent follow-up passes must drain.
    ingest_into("claude", &home, &data, false);
    assert_pending_drains("claude", &home, &data);
    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("unchanged since last index"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A changed source that temporarily parses empty is served from the last-good cache. That
/// guarded result must not publish the changed file's stat snapshot: otherwise a Windows
/// share-lock that later clears without another metadata update would make stale rows permanent.
#[test]
fn guarded_empty_reparse_does_not_publish_source_snapshot() {
    let home = temp_dir("source-guard-home");
    copy_dir(&fixture_home("claude"), &home);
    let file = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    let data = temp_dir("source-guard-data");

    ingest_into("claude", &home, &data, false);
    let before = normalize(&data);
    let source_body = fs::read(&file).unwrap();
    let published_source = fs::read(data.join(".source_snapshot.bin")).unwrap();
    assert!(!before.is_empty());
    assert!(data.join(".source_snapshot.bin").exists());

    // Empty is the adapter-independent stand-in for a read/share-lock failure: this is the
    // exact branch that retains the old cache entry after a changed file returns no rows.
    fs::write(&file, b"").unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(before, normalize(&data));
    assert_eq!(
        fs::read(data.join(".source_snapshot.bin")).unwrap(),
        published_source,
        "guarded stale rows must retain the last published baseline"
    );
    assert!(data.join(".ingest_pending.bin").exists());

    fs::write(&file, source_body).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(before, normalize(&data));
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A first-ever parse failure has no last-good fallback. It must abort before publishing even a
/// partial generation, retain its pending preflight, and let an ordinary retry build the first
/// complete cache/source generation without requiring a manual --full.
#[test]
fn first_generation_parse_failure_aborts_cleanly_and_normal_retry_recovers() {
    let home = temp_dir("cold-source-failure-home");
    copy_dir(&fixture_home("cline"), &home);
    let file = home
        .join(".cline")
        .join("data")
        .join("tasks")
        .join("1767348000000")
        .join("api_conversation_history.json");
    let source = fs::read(&file).unwrap();
    fs::write(&file, b"{").unwrap();
    let data = temp_dir("cold-source-failure-data");

    let failed = ingest_output("cline", &home, &data, false);
    assert!(
        !failed.status.success(),
        "unreadable cold source unexpectedly published"
    );
    assert!(
        String::from_utf8_lossy(&failed.stderr).contains("no generation was published"),
        "unexpected failure: {}",
        String::from_utf8_lossy(&failed.stderr)
    );
    for name in [
        "messages.jsonl",
        "replies.jsonl",
        "sessions.jsonl",
        agrep_core::cache::SESSION_FAMILY_META_FILE,
        ".ingest.sig",
        ".ingest_cache.bin",
        ".source_snapshot.bin",
    ] {
        assert!(
            !data.join(name).exists(),
            "cold failure left a partial publication: {name}"
        );
    }
    assert!(
        data.join(".ingest_pending.bin").exists(),
        "cold failure must leave its preflight for a guarded automatic retry"
    );

    fs::write(&file, source).unwrap();
    ingest_into("cline", &home, &data, false);
    assert!(normalize(&data).contains("build a cli flag parser"));
    assert!(data.join(".ingest_cache.bin").exists());
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn first_partial_generation_remains_warm_updatable() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("skip-unreadable-home");
    let tasks = home.join(".cline/data/tasks");
    let good = tasks.join("good/api_conversation_history.json");
    let bad = tasks.join("bad/api_conversation_history.json");
    fs::create_dir_all(good.parent().unwrap()).unwrap();
    fs::create_dir_all(bad.parent().unwrap()).unwrap();
    fs::write(
        &good,
        r#"[{"role":"user","content":"readable sibling survives","ts":1}]"#,
    )
    .unwrap();
    fs::write(
        &bad,
        r#"[{"role":"user","content":"unreadable sibling","ts":2}]"#,
    )
    .unwrap();
    fs::set_permissions(&bad, fs::Permissions::from_mode(0o000)).unwrap();
    let data = temp_dir("skip-unreadable-data");

    let output = ingest_output("cline", &home, &data, false);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "partial ingest failed:\n{stderr}");
    assert!(stderr.contains(&bad.to_string_lossy().to_string()));
    let published = normalize(&data);
    assert!(published.contains("readable sibling survives"));
    assert!(!published.contains("unreadable sibling"));
    // A stably denied file is a disclosed fact, not an in-flight condition: the
    // generation publishes, pending retires, and health names the denial.
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(data.join(".source-health.json").exists());

    fs::write(
        &good,
        r#"[{"role":"user","content":"readable sibling advances","ts":3}]"#,
    )
    .unwrap();
    let retry = ingest_output("cline", &home, &data, false);
    assert!(
        retry.status.success(),
        "partial retry failed:\n{}",
        String::from_utf8_lossy(&retry.stderr)
    );
    let retried = normalize(&data);
    assert!(retried.contains("readable sibling advances"));
    assert!(!retried.contains("readable sibling survives"));
    // The stably-denied sibling stays a disclosed fact: the advanced generation
    // publishes and retires pending on the warm pass too.
    assert!(!data.join(".ingest_pending.bin").exists());

    fs::set_permissions(&bad, fs::Permissions::from_mode(0o600)).unwrap();
    ingest_into("cline", &home, &data, false);
    let healed = normalize(&data);
    assert!(healed.contains("readable sibling advances"));
    assert!(healed.contains("unreadable sibling"));
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(!data.join(".source_absence_pending").exists());
    let _ = fs::remove_dir_all(home);
    let _ = fs::remove_dir_all(data);
}

#[cfg(unix)]
#[test]
fn persistent_unreadable_source_does_not_freeze_readable_warm_updates() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("warm-skip-unreadable-home");
    let tasks = home.join(".cline/data/tasks");
    let good = tasks.join("good/api_conversation_history.json");
    let bad = tasks.join("bad/api_conversation_history.json");
    fs::create_dir_all(good.parent().unwrap()).unwrap();
    fs::create_dir_all(bad.parent().unwrap()).unwrap();
    fs::write(
        &good,
        r#"[{"role":"user","content":"readable generation zero","ts":1}]"#,
    )
    .unwrap();
    fs::write(
        &bad,
        r#"[{"role":"user","content":"cached unreadable sibling","ts":2}]"#,
    )
    .unwrap();
    let data = temp_dir("warm-skip-unreadable-data");
    ingest_into("cline", &home, &data, false);
    let freshness_time = fs::metadata(data.join(".ingest.sig"))
        .unwrap()
        .modified()
        .unwrap();

    fs::set_permissions(&bad, fs::Permissions::from_mode(0o000)).unwrap();
    for generation in ["one", "two"] {
        fs::write(
            &good,
            format!(r#"[{{"role":"user","content":"readable generation {generation}","ts":1}}]"#),
        )
        .unwrap();
        let output = ingest_output("cline", &home, &data, false);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            output.status.success(),
            "partial warm ingest failed:\n{stderr}"
        );
        assert!(stderr.contains(&bad.to_string_lossy().to_string()));
        let published = normalize(&data);
        assert!(published.contains(&format!("readable generation {generation}")));
        assert!(published.contains("cached unreadable sibling"));
        // Each degraded generation publishes and retires pending; the freshness
        // clock keeps beating instead of freezing behind the denied sibling.
        assert!(!data.join(".ingest_pending.bin").exists());
        assert!(data.join(".source-health.json").exists());
        assert!(
            fs::metadata(data.join(".ingest.sig"))
                .unwrap()
                .modified()
                .unwrap()
                >= freshness_time,
        );
    }
    let steady = ingest_output("cline", &home, &data, false);
    assert!(steady.status.success());
    assert!(String::from_utf8_lossy(&steady.stdout).contains("unchanged since last index"));

    fs::set_permissions(&bad, fs::Permissions::from_mode(0o600)).unwrap();
    ingest_into("cline", &home, &data, false);
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(!data.join(".source-health.json").exists());
    let _ = fs::remove_dir_all(home);
    let _ = fs::remove_dir_all(data);
}

#[test]
fn repeated_empty_always_store_recovers_without_full() {
    let home = temp_dir("always-delete-home");
    let source = home.join(".cline/data/tasks/only/api_conversation_history.json");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::write(
        &source,
        r#"[{"role":"user","content":"delete this only source","ts":1}]"#,
    )
    .unwrap();
    let data = temp_dir("always-delete-data");
    ingest_into("cline", &home, &data, false);
    assert!(normalize(&data).contains("delete this only source"));

    fs::remove_file(&source).unwrap();
    ingest_into("cline", &home, &data, false);
    assert!(normalize(&data).contains("delete this only source"));
    assert!(data.join(".ingest_pending.bin").exists());

    ingest_into("cline", &home, &data, false);
    assert!(!normalize(&data).contains("delete this only source"));
    assert!(!data.join(".ingest_pending.bin").exists());
    let _ = fs::remove_dir_all(home);
    let _ = fs::remove_dir_all(data);
}

/// An exclusive lock held by the store's owner during a warm ingest must degrade to
/// serving the cache, byte-identical - never an error, never empty output.
#[test]
fn crush_locked_db_serves_cache() {
    let home = crush_home();
    let db = home
        .join(".local")
        .join("share")
        .join("crush")
        .join("crush.db");
    let data = temp_dir("crush-locked");

    ingest_into("crush", &home, &data, false);
    let before = normalize(&data);
    assert!(
        before.contains("convert the readme to asciidoc"),
        "fixture didn't ingest:\n{before}"
    );

    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute_batch("BEGIN EXCLUSIVE; UPDATE sessions SET updated_at = 1767349999000;")
        .unwrap();

    ingest_into("crush", &home, &data, false);
    let during = normalize(&data);
    assert_eq!(
        before, during,
        "locked store changed warm output instead of serving cache"
    );

    conn.execute_batch("ROLLBACK;").unwrap();
    conn.close().unwrap();
    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

/// Emit-rows on a converged store leaves its published-bytes pending handoff; the next
/// ordinary index proves the published generation current, retires the residue through the
/// all-hit shortcut, and never pays a crash-recovery derived rewrite for it.
#[test]
fn emit_rows_residue_on_converged_store_clears_via_shortcut() {
    let home = temp_dir("emit-residue-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("emit-residue-data");
    ingest_into("claude", &home, &data, true);
    let published = normalize(&data);
    let sentinel = data.join("emotions.jsonl");
    fs::write(&sentinel, b"enrichment sentinel\n").unwrap();

    let streamed = ingest_emit_output("claude", &home, &data);
    assert!(streamed.status.success());
    let streamed_stdout = String::from_utf8_lossy(&streamed.stdout);
    assert!(streamed_stdout.contains("files: stat"));
    assert!(streamed_stdout.contains("unchanged message set"));
    assert!(data.join(".ingest_pending.bin").exists());

    let next = ingest_output("claude", &home, &data, false);
    assert!(next.status.success());
    assert!(
        String::from_utf8_lossy(&next.stdout).contains("unchanged since last index"),
        "emit-rows residue forced a heavy pass:\n{}",
        String::from_utf8_lossy(&next.stdout)
    );
    assert!(!data.join(".ingest_pending.bin").exists());
    assert_eq!(normalize(&data), published);
    assert!(sentinel.exists(), "converged pass wiped turn enrichment");

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn emit_rows_reader_close_keeps_mode_and_publishes() {
    let home = temp_dir("emit-reader-close-home");
    let source = home.join(".claude/projects/stream/session.jsonl");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    let mut body = String::new();
    const ROWS: usize = 8_000;
    for turn in 0..ROWS {
        let text = if turn + 1 == ROWS {
            "late publication sentinel".to_string()
        } else {
            format!("streamed row {turn} {}", "x".repeat(96))
        };
        body.push_str(&format!(
            "{{\"type\":\"user\",\"userType\":\"external\",\"sessionId\":\"11111111-1111-4111-8111-111111111111\",\"timestamp\":\"2026-01-02T10:00:00.000Z\",\"cwd\":\"/work/stream\",\"message\":{{\"role\":\"user\",\"content\":{}}}}}\n",
            serde_json::to_string(&text).unwrap()
        ));
    }
    fs::write(&source, body).unwrap();
    let data = temp_dir("emit-reader-close-data");

    let mut command = Command::new(BIN);
    command.args(["index", "--agent", "claude", "--emit-rows"]);
    command
        .env("AGREP_HOME", &home)
        .env("AGREP_DATA_DIR", &data);
    for key in [
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "CLINE_DIR",
        "XDG_CONFIG_HOME",
        "AGREP_RS_BIN",
    ] {
        command.env_remove(key);
    }
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let stdout = child.stdout.take().unwrap();
    let mut reader = BufReader::new(stdout);
    loop {
        let mut line = String::new();
        assert_ne!(
            reader.read_line(&mut line).unwrap(),
            0,
            "stream ended before a row"
        );
        let value: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert!(
            value.get("total").is_some()
                || value.get("progress").is_some()
                || value.get("row").is_some()
        );
        if value.get("row").is_some() {
            break;
        }
    }
    drop(reader);
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "emit child failed after reader close: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(data.join(".ingest.sig").exists());
    assert!(data.join(".ingest_cache.bin").exists());
    assert!(fs::read_to_string(data.join("messages.jsonl"))
        .unwrap()
        .contains("late publication sentinel"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A symlinked store root is a durable unsupported fact: publication converges to the
/// all-hit shortcut with the issue on source health, and --full succeeds instead of
/// failing "will retry" forever.
#[cfg(unix)]
#[test]
fn symlinked_store_root_converges_with_health_instead_of_wedging() {
    let home = temp_dir("symlink-root-home");
    let elsewhere = temp_dir("symlink-root-elsewhere");
    copy_dir(&fixture_home("claude"), &elsewhere);
    fs::create_dir_all(home.join(".claude")).unwrap();
    std::os::unix::fs::symlink(
        elsewhere.join(".claude").join("projects"),
        home.join(".claude").join("projects"),
    )
    .unwrap();
    let data = temp_dir("symlink-root-data");

    let first = ingest_output("claude", &home, &data, true);
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(data.join(".source_snapshot.bin").exists());
    let health: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    assert_eq!(health["issues"][0]["kind"], "unsupported-link");

    let second = ingest_output("claude", &home, &data, false);
    assert!(
        second.status.success(),
        "{}",
        String::from_utf8_lossy(&second.stderr)
    );
    assert!(String::from_utf8_lossy(&second.stdout).contains("unchanged since last index"));

    let full = ingest_output("claude", &home, &data, true);
    assert!(
        full.status.success(),
        "--full wedged on a stable symlinked root: {}",
        String::from_utf8_lossy(&full.stderr)
    );
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&elsewhere);
    let _ = fs::remove_dir_all(&data);
}

/// One symlinked stray file inside a healthy store must not wedge the commit protocol:
/// siblings keep publishing, pending retires, --full succeeds, and removing the stray
/// clears source health again.
#[cfg(unix)]
#[test]
fn symlinked_stray_file_keeps_the_store_publishing() {
    let home = temp_dir("symlink-file-home");
    copy_dir(&fixture_home("claude"), &home);
    let data = temp_dir("symlink-file-data");
    ingest_into("claude", &home, &data, true);
    let published = normalize(&data);

    let outside = home.join("stray-target.jsonl");
    fs::write(&outside, b"{}\n").unwrap();
    let stray = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("stray.jsonl");
    std::os::unix::fs::symlink(&outside, &stray).unwrap();

    let degraded = ingest_output("claude", &home, &data, false);
    assert!(
        degraded.status.success(),
        "{}",
        String::from_utf8_lossy(&degraded.stderr)
    );
    assert!(!data.join(".ingest_pending.bin").exists());
    assert_eq!(normalize(&data), published);
    let health: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    assert_eq!(health["issues"][0]["kind"], "unsupported-link");

    let full = ingest_output("claude", &home, &data, true);
    assert!(
        full.status.success(),
        "--full wedged on a stable symlinked file: {}",
        String::from_utf8_lossy(&full.stderr)
    );
    assert_eq!(normalize(&data), published);

    fs::remove_file(&stray).unwrap();
    ingest_into("claude", &home, &data, false);
    assert!(!data.join(".source-health.json").exists());
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The battery the gates kept missing: a source whose bytes never stop moving while its
/// message set stays unchanged. A pass may publish or preserve the old generation with a
/// named unreadable-source failure; quiescence must drain pending without losing enrichment.
#[test]
fn publication_converges_under_a_continuously_appending_source() {
    let home = temp_dir("live-append-home");
    copy_dir(&fixture_home("claude"), &home);
    let target = home
        .join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl");
    let data = temp_dir("live-append-data");
    ingest_into("claude", &home, &data, true);
    let baseline = normalize(&data);
    let sentinel = data.join("emotions.jsonl");
    let sentinel_bytes = b"enrichment sentinel\n";
    fs::write(&sentinel, sentinel_bytes).unwrap();

    let running = Arc::new(AtomicBool::new(true));
    let writer_running = Arc::clone(&running);
    let writer_target = target.clone();
    let writer = std::thread::spawn(move || {
        let mut file = OpenOptions::new().append(true).open(writer_target).unwrap();
        while writer_running.load(Ordering::Acquire) {
            // blank lines move len/mtime/ctime every pass without adding a message
            file.write_all(b"\n").unwrap();
            file.flush().unwrap();
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
    });
    for pass in 0..4 {
        let output = ingest_output("claude", &home, &data, false);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        if output.status.success() {
            assert!(
                !stdout.contains("source validation incomplete"),
                "pass {pass} misread live drift as crash evidence:\n{stdout}"
            );
        } else {
            assert_eq!(output.status.code(), Some(1), "pass {pass}: {stderr}");
            assert!(
                stderr.contains("source-unreadable:"),
                "pass {pass}: {stderr}"
            );
            assert!(
                stderr.contains("freshness identity"),
                "pass {pass}: {stderr}"
            );
            assert!(
                stderr.contains("retained the old generation"),
                "pass {pass}: {stderr}"
            );
            assert!(
                data.join(".ingest_pending.bin").exists(),
                "pass {pass} lost the deferred source page"
            );
        }
        assert_eq!(
            normalize(&data),
            baseline,
            "pass {pass} changed the published baseline"
        );
        assert_eq!(
            fs::read(&sentinel).unwrap(),
            sentinel_bytes,
            "pass {pass} changed turn enrichment"
        );
    }
    running.store(false, Ordering::Release);
    writer.join().unwrap();

    let recovery = ingest_output("claude", &home, &data, false);
    assert!(
        recovery.status.success(),
        "quiet recovery failed: {}",
        String::from_utf8_lossy(&recovery.stderr)
    );
    assert_pending_drains("claude", &home, &data);
    assert_eq!(normalize(&data), baseline);
    assert_eq!(
        fs::read(&sentinel).unwrap(),
        sentinel_bytes,
        "quiet recovery changed turn enrichment"
    );
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(!data.join(".source-health.json").exists());
    let steady = ingest_output("claude", &home, &data, false);
    assert!(steady.status.success());
    assert!(
        String::from_utf8_lossy(&steady.stdout).contains("unchanged since last index"),
        "quiescence did not converge to the shortcut:\n{}",
        String::from_utf8_lossy(&steady.stdout)
    );
    assert_eq!(normalize(&data), baseline);
    assert_eq!(fs::read(&sentinel).unwrap(), sentinel_bytes);

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Deleting the FINAL Cursor conversation leaves a present, readable, validly-empty database.
/// One empty read must not delete (torn-read protection), but the repeated-observation
/// protocol must converge instead of retaining the stale rows forever.
#[test]
fn emptied_token_store_converges_after_repeated_observation() {
    let home = cursor_home();
    let db = home
        .join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb");
    let data = temp_dir("emptied-token-store-data");

    ingest_into("cursor", &home, &data, false);
    let baseline = normalize(&data);
    assert!(baseline.contains("the login test fails every third run"));

    // Delete every conversation but keep the database: present, readable, validly empty.
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute("DELETE FROM cursorDiskKV", []).unwrap();
    conn.close().unwrap();

    ingest_into("cursor", &home, &data, false);
    assert_eq!(
        normalize(&data),
        baseline,
        "one empty read deleted the store"
    );
    ingest_into("cursor", &home, &data, false);
    let deleted = normalize(&data);
    assert!(!deleted.contains("the login test fails every third run"));
    assert!(deleted.contains("=== messages.jsonl (0 lines) ==="));
    assert!(!data.join(".ingest_pending.bin").exists());

    let warm = ingest_output("cursor", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("unchanged since last index"));

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

/// `--full` used to refuse an emptied token store forever (cold cache, no confirmation path).
/// With the durable absence observation from a prior pass it is a valid confirming pass.
#[test]
fn full_reindex_accepts_a_confirmed_empty_token_store() {
    let home = cursor_home();
    let db = home
        .join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb");
    let data = temp_dir("emptied-token-store-full-data");

    ingest_into("cursor", &home, &data, false);
    let baseline = normalize(&data);
    assert!(baseline.contains("the login test fails every third run"));

    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute("DELETE FROM cursorDiskKV", []).unwrap();
    conn.close().unwrap();

    // First observation: withheld, baseline retained, the absence marker recorded.
    ingest_into("cursor", &home, &data, false);
    assert_eq!(normalize(&data), baseline);

    // The confirming pass may be --full: it must converge, not refuse forever.
    ingest_into("cursor", &home, &data, true);
    let deleted = normalize(&data);
    assert!(!deleted.contains("the login test fails every third run"));
    assert!(deleted.contains("=== messages.jsonl (0 lines) ==="));
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
}

/// Two claude projects, one session each, so a per-project loss is countable. Returns the
/// home plus each project directory.
fn two_project_home(tag: &str) -> (PathBuf, PathBuf, PathBuf) {
    let home = temp_dir(tag);
    let projects = home.join(".claude").join("projects");
    let alpha = projects.join("alpha");
    let beta = projects.join("beta");
    fs::create_dir_all(&alpha).unwrap();
    fs::create_dir_all(&beta).unwrap();
    let seed = fs::read_to_string(
        fixture_home("claude")
            .join(".claude/projects/proj-alpha")
            .join("sess-claude-0001.jsonl"),
    )
    .unwrap();
    fs::write(alpha.join("sess-claude-0001.jsonl"), &seed).unwrap();
    fs::write(
        beta.join("sess-claude-0002.jsonl"),
        seed.replace(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ),
    )
    .unwrap();
    (home, alpha, beta)
}

fn session_rows(data: &Path, session_prefix: &str) -> usize {
    sorted_lines(&data.join("messages.jsonl"))
        .iter()
        .filter(|line| line.contains(session_prefix))
        .count()
}

/// The blocker: `--full` beside a durably unreadable scope must publish neither a wipe nor a
/// wedge. Every lane keeps the readable project AND the denied one's last-good rows, exits 0,
/// and converges again once the permission returns. Covers a denied subdirectory and a denied
/// store root under both the implicit and `--full` lanes.
#[cfg(unix)]
#[test]
fn unreadable_scope_never_wipes_or_wedges_either_lane() {
    use std::os::unix::fs::PermissionsExt;

    for deny_root in [false, true] {
        let (home, alpha, _beta) = two_project_home("unreadable-scope-home");
        let projects = home.join(".claude").join("projects");
        let data = temp_dir("unreadable-scope-data");
        ingest_into("claude", &home, &data, false);
        assert_eq!(session_rows(&data, "11111111"), 2);
        assert_eq!(session_rows(&data, "22222222"), 2);

        let denied = if deny_root { &projects } else { &alpha };
        let restore = fs::metadata(denied).unwrap().permissions();
        fs::set_permissions(denied, fs::Permissions::from_mode(0o000)).unwrap();
        // implicit, --full, --full again, implicit: no lane may drop a published row
        for full in [false, true, true, false] {
            let output = ingest_output("claude", &home, &data, full);
            assert!(
                output.status.success(),
                "denied scope wedged the {} lane: {}",
                if full { "--full" } else { "implicit" },
                String::from_utf8_lossy(&output.stderr)
            );
            assert_eq!(
                session_rows(&data, "11111111"),
                2,
                "denied scope dropped the alpha generation (deny_root={deny_root}, full={full})"
            );
            assert_eq!(session_rows(&data, "22222222"), 2);
        }
        assert!(data.join(".source-health.json").exists());
        fs::set_permissions(denied, restore).unwrap();

        ingest_into("claude", &home, &data, false);
        assert_eq!(session_rows(&data, "11111111"), 2);
        assert_eq!(session_rows(&data, "22222222"), 2);
        assert!(!data.join(".source-health.json").exists());
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// A store root that becomes a symlink after a real generation exists is the same durable fact:
/// both lanes retain the published rows rather than republishing an empty store.
#[cfg(unix)]
#[test]
fn symlinked_store_root_retains_published_rows_in_both_lanes() {
    let (home, _alpha, _beta) = two_project_home("symlink-retain-home");
    let projects = home.join(".claude").join("projects");
    let data = temp_dir("symlink-retain-data");
    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 2);

    let moved = home.join("projects-elsewhere");
    fs::rename(&projects, &moved).unwrap();
    std::os::unix::fs::symlink(&moved, &projects).unwrap();

    for full in [false, true, true, false] {
        let output = ingest_output("claude", &home, &data, full);
        assert!(
            output.status.success(),
            "symlinked root wedged the {} lane: {}",
            if full { "--full" } else { "implicit" },
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            session_rows(&data, "11111111"),
            2,
            "symlinked root dropped the published generation (full={full})"
        );
        assert_eq!(session_rows(&data, "22222222"), 2);
    }

    fs::remove_file(&projects).unwrap();
    fs::rename(&moved, &projects).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 2);
    assert!(!data.join(".source-health.json").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The guard proves absence from an inventory, so absence of the inventory may not satisfy it.
/// With the parse cache gone there is nothing to serve the denied scope from, and `--full` must
/// retain the old generation instead of publishing the reduced one.
#[cfg(unix)]
#[test]
fn unprovable_scope_retains_the_old_generation_instead_of_publishing() {
    use std::os::unix::fs::PermissionsExt;

    let (home, alpha, _beta) = two_project_home("unprovable-scope-home");
    let data = temp_dir("unprovable-scope-data");
    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);

    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o000)).unwrap();
    fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
    let output = ingest_output("claude", &home, &data, true);
    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o755)).unwrap();

    assert!(
        !output.status.success(),
        "an unprovable scope published a reduced generation"
    );
    assert_eq!(
        normalize(&data),
        published,
        "a refused publication still mutated the derived generation"
    );

    // The permission is the user's to grant; granting it converges without a repair command.
    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 2);
    assert_eq!(session_rows(&data, "22222222"), 2);
    assert!(!data.join(".ingest_pending.bin").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A virgin store has no generation to lose, so a durable denial there is not a loss to guard:
/// the first ingest must still publish (empty) rather than wedge on an unprovable scope.
#[cfg(unix)]
#[test]
fn first_ever_ingest_under_a_denied_root_publishes_instead_of_wedging() {
    use std::os::unix::fs::PermissionsExt;

    let (home, _alpha, _beta) = two_project_home("virgin-denied-home");
    let projects = home.join(".claude").join("projects");
    let data = temp_dir("virgin-denied-data");
    fs::set_permissions(&projects, fs::Permissions::from_mode(0o000)).unwrap();

    for full in [false, true] {
        let output = ingest_output("claude", &home, &data, full);
        assert!(
            output.status.success(),
            "virgin denied root wedged the {} lane: {}",
            if full { "--full" } else { "implicit" },
            String::from_utf8_lossy(&output.stderr)
        );
    }
    assert!(data.join(".source-health.json").exists());

    fs::set_permissions(&projects, fs::Permissions::from_mode(0o755)).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 2);
    assert_eq!(session_rows(&data, "22222222"), 2);
    assert!(!data.join(".source-health.json").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Retaining under an unreadable scope must not blunt real deletion convergence: a project the
/// user actually removed still tombstones under `--full`, even while a sibling stays denied.
#[cfg(unix)]
#[test]
fn a_real_deletion_still_converges_beside_a_denied_sibling() {
    use std::os::unix::fs::PermissionsExt;

    let (home, alpha, beta) = two_project_home("deletion-beside-denial-home");
    let data = temp_dir("deletion-beside-denial-data");
    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 2);

    fs::remove_dir_all(&alpha).unwrap();
    fs::set_permissions(&beta, fs::Permissions::from_mode(0o000)).unwrap();
    let output = ingest_output("claude", &home, &data, true);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        session_rows(&data, "11111111"),
        0,
        "a genuinely deleted project failed to tombstone"
    );
    assert_eq!(
        session_rows(&data, "22222222"),
        2,
        "the denied sibling lost its published rows"
    );

    fs::set_permissions(&beta, fs::Permissions::from_mode(0o755)).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 0);
    assert_eq!(session_rows(&data, "22222222"), 2);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The upgrade path that wedged a real box: an old build's parse cache this build cannot decode
/// beside an untouched healthy store. Recovery discards its own broken artifact and publishes
/// every row in one implicit pass - the flag it used to demand bought the user nothing.
#[test]
fn undecodable_cache_beside_a_healthy_store_recovers_without_a_flag() {
    let (home, _alpha, _beta) = two_project_home("undecodable-upgrade-home");
    let data = temp_dir("undecodable-upgrade-data");
    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);

    force_ambiguous_event_recovery(&data);
    let recovered = ingest_output("claude", &home, &data, false);
    assert!(
        recovered.status.success(),
        "an undecodable cache wedged a healthy store: {}",
        String::from_utf8_lossy(&recovered.stderr)
    );
    assert!(
        String::from_utf8_lossy(&recovered.stdout).contains("discarded an undecodable parse cache"),
        "the discard was not disclosed"
    );
    assert_eq!(
        normalize(&data),
        published,
        "an automatic cache rebuild changed the published generation"
    );
    assert_eq!(session_rows(&data, "11111111"), 2);
    assert_eq!(session_rows(&data, "22222222"), 2);
    assert!(data.join(".events_complete.claude.json").exists());
    assert!(!data.join(".ingest_pending.bin").exists());
    assert!(!data.join(".source-health.json").exists());

    let warm = ingest_output("claude", &home, &data, false);
    assert!(warm.status.success());
    assert!(String::from_utf8_lossy(&warm.stdout).contains("skipped ingest + writes"));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A discarded base holds no last-good rows, so one clean absence must not converge a whole
/// store the published generation held: the pass retains it, and only the repeated preflight
/// observation retires it. Row loss and the deletion protocol both survive the discard.
#[test]
fn undecodable_cache_recovery_needs_two_observations_for_a_vanished_store() {
    let (home, _alpha, _beta) = two_project_home("undecodable-vanished-home");
    let projects = home.join(".claude").join("projects");
    let moved = home.join("projects-elsewhere");
    let data = temp_dir("undecodable-vanished-data");
    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);

    force_ambiguous_event_recovery(&data);
    fs::rename(&projects, &moved).unwrap();
    let first = ingest_output("claude", &home, &data, false);
    assert_retained_generation_refusal(&first);
    assert_eq!(
        normalize(&data),
        published,
        "a single absent observation dropped the published store"
    );
    assert!(data.join(".ingest_pending.bin").exists());

    let second = ingest_output("claude", &home, &data, false);
    assert!(
        second.status.success(),
        "the repeated observation failed to converge: {}",
        String::from_utf8_lossy(&second.stderr)
    );
    assert_eq!(session_rows(&data, "11111111"), 0);
    assert_eq!(session_rows(&data, "22222222"), 0);

    fs::rename(&moved, &projects).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 2);
    assert_eq!(session_rows(&data, "22222222"), 2);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The row-loss guard outranks the discard: with no cache to serve a denied project from, the
/// pass may not publish the reduced generation, and granting the permission converges it.
#[cfg(unix)]
#[test]
fn undecodable_cache_recovery_still_refuses_a_reduced_generation() {
    use std::os::unix::fs::PermissionsExt;

    let (home, alpha, _beta) = two_project_home("undecodable-reduced-home");
    let data = temp_dir("undecodable-reduced-data");
    ingest_into("claude", &home, &data, false);
    let published = normalize(&data);

    force_ambiguous_event_recovery(&data);
    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o000)).unwrap();
    let refused = ingest_output("claude", &home, &data, false);
    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o755)).unwrap();
    assert_retained_generation_refusal(&refused);
    assert_eq!(
        normalize(&data),
        published,
        "an unprovable scope published a reduced generation after a cache discard"
    );

    ingest_into("claude", &home, &data, false);
    assert_eq!(session_rows(&data, "11111111"), 2);
    assert_eq!(session_rows(&data, "22222222"), 2);
    assert!(!data.join(".ingest_pending.bin").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The same protocol for a whole-store adapter, whose rows live in one snapshot rather than
/// per-file entries: a discarded base cannot serve them, so a clean absence retains the
/// generation once and converges only on the repeated observation.
#[test]
fn undecodable_cache_recovery_needs_two_observations_for_a_vanished_snapshot_store() {
    let home = temp_dir("undecodable-vanished-snapshot-home");
    copy_dir(&fixture_home("cline"), &home);
    let store = home.join(".cline");
    let moved = home.join("cline-elsewhere");
    let data = temp_dir("undecodable-vanished-snapshot-data");
    ingest_into("cline", &home, &data, false);
    let published = normalize(&data);
    assert!(
        !fs::read_to_string(data.join("messages.jsonl"))
            .unwrap()
            .is_empty(),
        "the cline fixture published no rows to protect"
    );

    corrupt_current_cache_payload(&data);
    fs::remove_file(data.join(".events_complete.cline.json")).unwrap();
    fs::rename(&store, &moved).unwrap();
    let first = ingest_output("cline", &home, &data, false);
    assert_retained_generation_refusal(&first);
    assert_eq!(
        normalize(&data),
        published,
        "a single absent observation dropped a whole-store adapter"
    );

    let second = ingest_output("cline", &home, &data, false);
    assert!(
        second.status.success(),
        "the repeated observation failed to converge: {}",
        String::from_utf8_lossy(&second.stderr)
    );
    assert!(
        fs::read_to_string(data.join("messages.jsonl"))
            .unwrap()
            .is_empty(),
        "a confirmed store deletion left rows behind"
    );

    fs::rename(&moved, &store).unwrap();
    ingest_into("cline", &home, &data, false);
    assert_eq!(normalize(&data), published);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A scope that publishes as an issue record contributes no path to the next generation's
/// inventory. That silence must never read as "this scope held nothing": with the parse cache
/// also discarded, the run has no witness for the denied scope and must not publish past it.
#[test]
#[cfg(unix)]
fn denied_scope_absent_from_the_published_inventory_never_licenses_dropping_its_rows() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("denied-inventory-home");
    copy_dir(&fixture_home("claude"), &home);
    let projects = home.join(".claude/projects");
    let alpha = projects.join("proj-alpha");

    // A second, readable scope so later passes have real work and cannot short-circuit.
    let beta = projects.join("proj-beta");
    fs::create_dir_all(&beta).unwrap();
    let seed = fs::read(alpha.join("sess-claude-0001.jsonl")).unwrap();
    let rekey = |to: &[u8]| replace_bytes(&seed, b"11111111-1111-4111-8111-111111111111", to);
    fs::write(
        beta.join("sess-claude-0002.jsonl"),
        rekey(b"22222222-2222-4222-8222-222222222222"),
    )
    .unwrap();

    let data = temp_dir("denied-inventory-data");
    ingest_into("claude", &home, &data, true);
    let baseline = msg_keys(&data);
    let alpha_rows: HashSet<_> = baseline
        .iter()
        .filter(|(session, _, _)| session.starts_with("11111111"))
        .cloned()
        .collect();
    assert!(
        !alpha_rows.is_empty(),
        "fixture must publish rows for the scope about to be denied"
    );

    // Deny the scope. This pass correctly retains its rows and republishes the source
    // snapshot with proj-alpha reduced to an issue record that carries no path.
    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o000)).unwrap();
    let denied = ingest_output("claude", &home, &data, false);
    assert!(
        denied.status.success(),
        "a stably denied scope must still publish: {}",
        String::from_utf8_lossy(&denied.stderr)
    );
    assert_eq!(
        msg_keys(&data),
        baseline,
        "the denied scope's rows were dropped by the retaining pass"
    );

    // Discard the last-good rows the way a torn write would: corrupt the payload while the
    // header (and its writing-build identity) stays intact, so no owner guard intervenes.
    let cache_path = data.join(".ingest_cache.bin");
    let mut cache = fs::read(&cache_path).unwrap();
    let tail = cache.len() - 32;
    for byte in &mut cache[tail..] {
        *byte ^= 0xFF;
    }
    fs::write(&cache_path, &cache).unwrap();

    // Real work, so the run reaches ingest instead of the unchanged-signature short circuit.
    fs::write(
        beta.join("sess-claude-0003.jsonl"),
        rekey(b"33333333-3333-4333-8333-333333333333"),
    )
    .unwrap();

    let blind = ingest_output("claude", &home, &data, false);
    let published = msg_keys(&data);
    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o755)).unwrap();

    let missing: Vec<_> = alpha_rows.difference(&published).collect();
    assert!(
        missing.is_empty(),
        "rows for the unreadable scope were dropped (exit {:?}); the published inventory's \
         silence about an issue-only scope was read as proof it held nothing:\n{}\nlost: {missing:?}",
        blind.status.code(),
        String::from_utf8_lossy(&blind.stderr)
    );
    assert!(
        published.len() >= baseline.len(),
        "the corpus shrank while a scope was unreadable: {} -> {}",
        baseline.len(),
        published.len()
    );
    if !blind.status.success() {
        assert_eq!(
            published, baseline,
            "a refusing pass must leave the old generation exactly as it was"
        );
    }

    // The denial was never real data loss: restoring access converges without --full.
    ingest_into("claude", &home, &data, false);
    assert!(
        alpha_rows.is_subset(&msg_keys(&data)),
        "the scope's rows did not return once it became readable again"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The A2 wedge: a durably denied scope plus an undecodable parse cache. Every refusal must
/// name only levers this state can satisfy (no "--full", no "will retry"), the discard must
/// be disclosed in the retry and --full lanes too, and restoring access must heal everything.
#[test]
#[cfg(unix)]
fn denied_scope_with_lost_cache_names_only_satisfiable_recovery_and_heals_on_restore() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("wedge-honest-home");
    copy_dir(&fixture_home("claude"), &home);
    let projects = home.join(".claude/projects");
    let alpha = projects.join("proj-alpha");
    let beta = projects.join("proj-beta");
    fs::create_dir_all(&beta).unwrap();
    let seed = fs::read(alpha.join("sess-claude-0001.jsonl")).unwrap();
    let rekey = |to: &[u8]| replace_bytes(&seed, b"11111111-1111-4111-8111-111111111111", to);
    fs::write(
        beta.join("sess-claude-0002.jsonl"),
        rekey(b"22222222-2222-4222-8222-222222222222"),
    )
    .unwrap();

    let data = temp_dir("wedge-honest-data");
    ingest_into("claude", &home, &data, true);
    let baseline = msg_keys(&data);
    let alpha_rows: HashSet<_> = baseline
        .iter()
        .filter(|(session, _, _)| session.starts_with("11111111"))
        .cloned()
        .collect();
    assert!(!alpha_rows.is_empty());

    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o000)).unwrap();
    ingest_into("claude", &home, &data, false);
    assert_eq!(msg_keys(&data), baseline);

    // Undecodable, with the writing-build header intact so no owner guard intervenes.
    let cache_path = data.join(".ingest_cache.bin");
    let mut cache = fs::read(&cache_path).unwrap();
    let tail = cache.len() - 32;
    for byte in &mut cache[tail..] {
        *byte ^= 0xFF;
    }
    fs::write(&cache_path, &cache).unwrap();
    fs::write(
        beta.join("sess-claude-0003.jsonl"),
        rekey(b"33333333-3333-4333-8333-333333333333"),
    )
    .unwrap();

    let assert_honest_refusal = |output: &std::process::Output, label: &str| {
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        assert!(!output.status.success(), "{label} unexpectedly published");
        assert!(
            stderr.contains("retained the old generation"),
            "{label} refusal changed shape:\n{stderr}"
        );
        assert!(
            stderr.contains("readable again"),
            "{label} refusal hides the one real lever:\n{stderr}"
        );
        assert!(
            !stderr.contains("--full") && !stderr.contains("will retry"),
            "{label} refusal advertises a lever this state cannot satisfy:\n{stderr}"
        );
        assert_eq!(msg_keys(&data), baseline, "{label} mutated the generation");
    };

    let wedge = ingest_output("claude", &home, &data, false);
    assert_honest_refusal(&wedge, "wedge run");

    let full = ingest_output("claude", &home, &data, true);
    assert_honest_refusal(&full, "--full run");
    assert!(
        String::from_utf8_lossy(&full.stdout).contains("discarded an undecodable parse cache"),
        "--full retention lane dropped the discard disclosure:\n{}",
        String::from_utf8_lossy(&full.stdout)
    );

    let retry = ingest_output("claude", &home, &data, false);
    assert_honest_refusal(&retry, "retry run");
    assert!(
        String::from_utf8_lossy(&retry.stdout).contains("discarded an undecodable parse cache"),
        "guarded retry lane dropped the discard disclosure:\n{}",
        String::from_utf8_lossy(&retry.stdout)
    );

    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o755)).unwrap();
    ingest_into("claude", &home, &data, false);
    let healed = msg_keys(&data);
    assert!(
        alpha_rows.is_subset(&healed),
        "restored scope's rows did not return"
    );
    assert!(
        healed
            .iter()
            .any(|(session, _, _)| session.starts_with("33333333")),
        "drift observed during the denial was never published after restore"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A discarded base may not buy a deletion a cheaper proof through the guarded-retry lane:
/// the invariant binds wherever the base was built, so one observation retains and only the
/// second stable observation converges the deletion. The discard is disclosed here too.
#[test]
fn retry_with_discarded_base_defers_a_deletion_to_the_second_stable_observation() {
    let home = temp_dir("retry-discard-delete-home");
    copy_dir(&fixture_home("claude"), &home);
    let projects = home.join(".claude/projects");
    let beta = projects.join("proj-beta");
    fs::create_dir_all(&beta).unwrap();
    let seed = fs::read(projects.join("proj-alpha").join("sess-claude-0001.jsonl")).unwrap();
    let deletable = beta.join("sess-claude-0002.jsonl");
    fs::write(
        &deletable,
        replace_bytes(
            &seed,
            b"11111111-1111-4111-8111-111111111111",
            b"22222222-2222-4222-8222-222222222222",
        ),
    )
    .unwrap();

    let data = temp_dir("retry-discard-delete-data");
    ingest_into("claude", &home, &data, true);
    let baseline = msg_keys(&data);
    assert!(baseline
        .iter()
        .any(|(session, _, _)| session.starts_with("22222222")));

    // Enter the source-retry lane with a discarded base: a pending marker left by a prior
    // pass, an undecodable cache, and a deletion that has only one observation behind it.
    fs::copy(
        data.join(".source_snapshot.bin"),
        data.join(".ingest_pending.bin"),
    )
    .unwrap();
    corrupt_current_cache_payload(&data);
    fs::remove_file(&deletable).unwrap();

    let first = ingest_output("claude", &home, &data, false);
    assert!(
        !first.status.success(),
        "one observation deleted published rows through a discarded base:\n{}",
        String::from_utf8_lossy(&first.stdout)
    );
    assert!(
        String::from_utf8_lossy(&first.stdout).contains("discarded an undecodable parse cache"),
        "guarded retry dropped the discard disclosure:\n{}",
        String::from_utf8_lossy(&first.stdout)
    );
    assert_eq!(
        msg_keys(&data),
        baseline,
        "refusing pass mutated the generation"
    );

    ingest_into("claude", &home, &data, false);
    let converged = msg_keys(&data);
    assert!(
        !converged
            .iter()
            .any(|(session, _, _)| session.starts_with("22222222")),
        "second stable observation failed to converge the deletion"
    );
    assert!(!data.join(".ingest_pending.bin").exists());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Event repair with no cache at all keeps the two-snapshot protocol, but the pair may be
/// stably incomplete: a byte-identical denied preflight is accepted as the second
/// observation, and the surviving refusal names access - not the parse cache - as the cause.
#[test]
#[cfg(unix)]
fn cacheless_event_repair_accepts_a_stable_incomplete_pair_and_refuses_honestly() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("stable-incomplete-pair-home");
    copy_dir(&fixture_home("claude"), &home);
    let alpha = home.join(".claude/projects/proj-alpha");
    let data = temp_dir("stable-incomplete-pair-data");
    ingest_into("claude", &home, &data, true);
    let baseline = msg_keys(&data);

    force_cacheless_event_recovery(&data);
    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o000)).unwrap();

    let first = ingest_output("claude", &home, &data, false);
    assert!(!first.status.success());
    let first_err = String::from_utf8_lossy(&first.stderr).to_string();
    assert!(
        first_err.contains("two stable source snapshots"),
        "first observation refusal changed shape:\n{first_err}"
    );
    assert!(
        first_err.contains("readable again") && !first_err.contains("--full"),
        "cacheless repair under a durable denial advertises a dead lever:\n{first_err}"
    );

    let second = ingest_output("claude", &home, &data, false);
    assert!(
        !second.status.success(),
        "publication slipped past a denied scope"
    );
    let second_err = String::from_utf8_lossy(&second.stderr).to_string();
    assert!(
        !second_err.contains("parse cache"),
        "a stable incomplete pair was not accepted as the second observation:\n{second_err}"
    );
    assert!(
        second_err.contains("readable again"),
        "the surviving refusal lost the real lever:\n{second_err}"
    );
    assert_eq!(msg_keys(&data), baseline);

    fs::set_permissions(&alpha, fs::Permissions::from_mode(0o755)).unwrap();
    let healed = ingest_output("claude", &home, &data, false);
    if !healed.status.success() {
        ingest_into("claude", &home, &data, false);
    }
    assert_eq!(
        msg_keys(&data),
        baseline,
        "restore did not heal the generation"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn cursor_schema_migration_is_unknown_not_a_proven_empty_store() {
    let home = cursor_home();
    let data = temp_dir("cursor-schema-absent-data");
    let db = home
        .join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb");

    ingest_into("cursor", &home, &data, true);
    let baseline = msg_keys(&data);
    assert!(!baseline.is_empty(), "cold cursor ingest published no rows");

    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute_batch("ALTER TABLE cursorDiskKV RENAME TO cursorDiskKV_v2;")
        .unwrap();
    let on_disk: i64 = conn
        .query_row(
            "SELECT count(*) FROM cursorDiskKV_v2 \
             WHERE key >= 'composerData:' AND key < 'composerData;'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    conn.close().unwrap();
    assert!(
        on_disk > 0,
        "the migration moved the conversations, it did not delete them"
    );

    // The two-observation absence protocol converges on the second agreeing pass, so a
    // single warm run proves nothing; run past it.
    for pass in 1..=3 {
        let out = ingest_output("cursor", &home, &data, false);
        assert_eq!(
            msg_keys(&data),
            baseline,
            "warm pass {pass} erased {on_disk} indexed cursor conversations that a schema \
             migration only hid (exit {:?}):\n{}",
            out.status.code(),
            String::from_utf8_lossy(&out.stderr)
        );
    }

    // The distinction is structural, not blanket caution: a table that IS there and
    // enumerates zero rows is a real emptying and must still converge.
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute_batch(
        "ALTER TABLE cursorDiskKV_v2 RENAME TO cursorDiskKV; DELETE FROM cursorDiskKV;",
    )
    .unwrap();
    conn.close().unwrap();
    let mut converged = false;
    for _ in 0..4 {
        ingest_output("cursor", &home, &data, false);
        if msg_keys(&data).is_empty() {
            converged = true;
            break;
        }
    }
    assert!(
        converged,
        "an enumerable, genuinely emptied cursor store never converged; schema-absence \
         caution over-reached into 'never deletable'"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// An opencode child-session row (or query) the reader cannot decode leaves that subagent
/// link unobserved. An unobserved link is not a removed one, so it may not license the
/// wholesale replacement that deletes the last-good event.

#[test]
fn opencode_undecodable_child_scope_retains_last_good_subagent_events() {
    fn subagent_sessions(data: &Path) -> usize {
        event_rows(data)
            .into_iter()
            .filter(|(_, payload)| String::from_utf8_lossy(payload).contains("subagent_start"))
            .count()
    }

    let home = opencode_home();
    let data = temp_dir("opencode-child-decode-data");
    let db = home
        .join(".local")
        .join("share")
        .join("opencode")
        .join("opencode.db");

    ingest_into("opencode", &home, &data, true);
    let baseline = subagent_sessions(&data);
    let messages = msg_keys(&data);
    assert!(
        baseline > 0,
        "cold opencode ingest published no subagent link"
    );

    // One row that fails to decode: the child session is still on disk, only its
    // timestamp stopped being an integer.
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute(
        "UPDATE session SET time_created = NULL WHERE id = 'sess-oc-child'",
        [],
    )
    .unwrap();
    conn.close().unwrap();
    let out = ingest_output("opencode", &home, &data, false);
    assert_eq!(
        subagent_sessions(&data),
        baseline,
        "one undecodable child row deleted the last-good subagent events (exit {:?}):\n{}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        messages.is_subset(&msg_keys(&data)),
        "the uncovered child scope also cost the store its messages"
    );

    // Same shape at whole-scope width: a child-session QUERY that cannot run takes out
    // every subagent link at once.
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute_batch("ALTER TABLE session RENAME COLUMN title TO label;")
        .unwrap();
    conn.close().unwrap();
    let out = ingest_output("opencode", &home, &data, false);
    assert_eq!(
        subagent_sessions(&data),
        baseline,
        "a failing child-session query wiped every subagent link in one pass (exit {:?}):\n{}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}
