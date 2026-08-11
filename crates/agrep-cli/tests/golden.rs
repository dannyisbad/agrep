//! Golden fixture harness: run the real ingest binary over a synthetic fixture home and
//! diff its normalized output against a checked-in golden. The fixtures make intentional parser
//! changes explicit and catch accidental output drift.
//!
//! Run:         cargo test -p agrep-ingest --test golden
//! Regenerate:  UPDATE_GOLDEN=1 cargo test -p agrep-ingest --test golden   (after an intended change)
//!
//! The seam is AGREP_HOME (ingest::home() honors it, highest priority): each case points
//! discovery at tests/fixtures/<adapter>/home and writes output to a fresh tempdir. A
//! subprocess per case means no shared global state (home-leaf cache, env) can bleed
//! between adapters, and the run never touches the real stores. Every var that could leak
//! a real store or the installed binary is stripped from the child env.
//!
//! Shared machinery (normalization, scrubbed-env ingest, the intake identity) lives in
//! tests/common/mod.rs, also used by the torture suite (torture.rs).

mod common;

use common::*;
use std::fs;
use std::path::Path;

/// Compare against the checked-in golden, or rewrite it under UPDATE_GOLDEN=1. On mismatch,
/// report the section, first differing line, and a little context - not just "mismatch".
fn check(agent: &str, home: &Path) {
    let got = run_ingest(agent, home);
    let golden = fixtures_dir().join(agent).join("golden.txt");
    if std::env::var_os("UPDATE_GOLDEN").is_some() {
        fs::write(&golden, &got).unwrap();
        eprintln!("  updated golden: {}", golden.display());
        return;
    }
    let want = fs::read_to_string(&golden)
        .unwrap_or_else(|_| panic!("no golden for {agent}; run UPDATE_GOLDEN=1 to create it"));
    if got == want {
        return;
    }
    let gl: Vec<&str> = got.lines().collect();
    let wl: Vec<&str> = want.lines().collect();
    let mut section = String::from("(start)");
    for i in 0..gl.len().max(wl.len()) {
        let g = gl.get(i).copied().unwrap_or("<EOF>");
        let w = wl.get(i).copied().unwrap_or("<EOF>");
        if let Some(h) = w.strip_prefix("=== ") {
            section = h.to_string();
        }
        if g != w {
            let mut ctx = String::new();
            for j in i.saturating_sub(2)..i {
                ctx.push_str(&format!("      {}\n", wl.get(j).copied().unwrap_or("")));
            }
            panic!(
                "golden mismatch: {agent}, section [{section}], first diff at line {i}\n{ctx}  want: {w}\n   got: {g}\n\nline counts: want={} got={}\nrun `UPDATE_GOLDEN=1 cargo test -p agrep-ingest --test golden` after an intended change.",
                wl.len(),
                gl.len()
            );
        }
    }
}

#[test]
fn golden_claude() {
    check("claude", &fixture_home("claude"));
}

#[test]
fn golden_codex() {
    check("codex", &fixture_home("codex"));
}

#[test]
fn golden_antigravity() {
    check("antigravity", &fixture_home("antigravity"));
}

#[test]
fn golden_kimi() {
    check("kimi", &fixture_home("kimi"));
}

#[test]
fn golden_pi() {
    check("pi", &fixture_home("pi"));
}

#[test]
fn golden_cline() {
    check("cline", &fixture_home("cline"));
}

#[test]
fn golden_opencode() {
    let home = opencode_home();
    check("opencode", &home);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn golden_gemini() {
    check("gemini", &fixture_home("gemini"));
}

#[test]
fn golden_crush() {
    let home = crush_home();
    check("crush", &home);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn golden_cursor() {
    let home = cursor_home();
    check("cursor", &home);
    let _ = fs::remove_dir_all(&home);
}

#[test]
fn cursor_unrelated_workspace_schema_does_not_block_global_store() {
    let home = cursor_home();
    let workspace = home
        .join(".config")
        .join("Cursor")
        .join("User")
        .join("workspaceStorage")
        .join("eeee0001ffff0002aaaa0003bbbb0004");
    fs::create_dir_all(&workspace).unwrap();
    fs::write(
        workspace.join("workspace.json"),
        r#"{"folder":"file:///c%3A/Users/tester/Desktop/unrelated"}"#,
    )
    .unwrap();
    let conn = rusqlite::Connection::open(workspace.join("state.vscdb")).unwrap();
    conn.execute("CREATE TABLE unrelated(key TEXT, value BLOB)", [])
        .unwrap();
    conn.close().unwrap();

    let got = run_ingest("cursor", &home);
    let _ = fs::remove_dir_all(&home);
    assert!(
        got.contains("the login test fails every third run")
            && got.contains(r#""project":"flaky-app""#)
            && got.contains("does cursor keep chat history locally"),
        "unrelated workspace schema blocked valid Cursor global history:\n{got}"
    );
}

/// Cursor records no reliable per-session update timestamp, so the token hashes composer and
/// bubble rows. An in-place bubble edit with unchanged ids must still trigger a reparse.
#[test]
fn cursor_token_catches_edited_bubble_same_count() {
    let home = cursor_home();
    let db = home
        .join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb");
    let data = temp_dir("cursor-token");

    ingest_into("cursor", &home, &data, false);
    let before = normalize(&data);
    assert!(
        before.contains("the login test fails every third run"),
        "fixture didn't ingest:\n{before}"
    );

    // edit the user bubble IN PLACE; composerData (headers, timestamps) is untouched
    {
        let conn = rusqlite::Connection::open(&db).unwrap();
        conn.execute(
            "UPDATE cursorDiskKV SET value = ? WHERE key = 'bubbleId:c1a11111-1111-4111-8111-111111111111:b1b11111-1111-4111-8111-111111111111'",
            [r#"{"bubbleId":"b1b11111-1111-4111-8111-111111111111","type":1,"text":"the checkout test fails every third run, figure out why","createdAt":"2026-03-01T12:00:00.000Z"}"#],
        )
        .unwrap();
        conn.close().unwrap();
    }

    ingest_into("cursor", &home, &data, false);
    let after = normalize(&data);
    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);

    assert!(
        after.contains("the checkout test fails") && !after.contains("the login test fails"),
        "cursor token missed an edited-bubble-same-count change:\n--- before ---\n{before}\n--- after ---\n{after}"
    );
}

/// An edited message with the same message count and last id must be re-ingested. Crush's token is the
/// session's updated_at; editing a bubble bumps it (as crush's own trigger does), so the
/// per-conversation token cache reparses that session. Also proves --full is the reset path:
/// an edit WITHOUT bumping updated_at is (correctly) not picked up incrementally, but --full
/// re-reads it.
#[test]
fn crush_token_catches_edited_bubble_same_count() {
    let home = crush_home();
    let db = home
        .join(".local")
        .join("share")
        .join("crush")
        .join("crush.db");
    let data = temp_dir("crush-token");

    // run 1 (warm): populates the per-conversation token cache + output
    ingest_into("crush", &home, &data, false);
    let before = normalize(&data);
    assert!(
        before.contains("convert the readme to asciidoc"),
        "fixture didn't ingest:\n{before}"
    );

    // edit sc1's user bubble IN PLACE - same message count, same ids - and bump the session's
    // updated_at exactly as crush's update trigger would.
    {
        let conn = rusqlite::Connection::open(&db).unwrap();
        conn.execute(
            "UPDATE messages SET parts = ? WHERE id = 'm1'",
            [r#"[{"type":"text","data":{"text":"convert the readme to restructuredtext"}}]"#],
        )
        .unwrap();
        conn.execute(
            "UPDATE sessions SET updated_at = 1767348999000 WHERE id = 'sc1'",
            [],
        )
        .unwrap();
        conn.close().unwrap();
    }

    // run 2 (warm): the moved token must reparse sc1 and surface the edit
    ingest_into("crush", &home, &data, false);
    let after = normalize(&data);
    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);

    assert!(
        after.contains("convert the readme to restructuredtext")
            && !after.contains("convert the readme to asciidoc"),
        "Token fingerprint missed an edited-bubble-same-count change:\n--- before ---\n{before}\n--- after ---\n{after}"
    );
}

/// A warm ingest whose store has gone away must serve the last-good cached output, not empty;
/// an empty result cascades to session-index rewrite
/// -> corpus removal-reconciliation -> event-file unlink. Uses opencode (the sqlite adapter,
/// the motivating locking case): ingest once warm, remove the db, ingest again into the same
/// data dir, and require byte-identical output on the first absent observation.
#[test]
fn absent_store_serves_cache_not_empty() {
    let home = opencode_home();
    let data = temp_dir("opencode-absent-store");
    let db = home
        .join(".local")
        .join("share")
        .join("opencode")
        .join("opencode.db");

    // run 1: cold cache -> parses the store, writes output + the parse cache into `data`
    ingest_into("opencode", &home, &data, false);
    let before = normalize(&data);
    assert!(
        before.contains("convert config to yaml"),
        "fixture didn't ingest on the first run:\n{before}"
    );

    // A single absent observation must be treated as a transient unmount.
    let stashed = db.with_extension("db.away");
    fs::rename(&db, &stashed).unwrap();

    // run 2: warm cache, store gone -> the driver must serve the cached messages
    ingest_into("opencode", &home, &data, false);
    let after = normalize(&data);

    let _ = fs::remove_dir_all(&data);
    let _ = fs::remove_dir_all(&home);
    assert_eq!(
        before, after,
        "an absent store changed the output instead of serving cache\n\
         --- before ---\n{before}\n--- after ---\n{after}"
    );
}
