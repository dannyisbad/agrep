//! Cline keeps tasks under every editor root; crush keeps one database per data root. Removing
//! one root while a sibling stays live is not the whole-store absence the two-observation
//! protocol guards: the remaining root reads complete, so the removed root's rows go on the next
//! pass in every lane and return, reparsed, when the root does. Nothing wedges, refuses, or keeps
//! the rows forever; a temporary unmount costs those rows for its duration only.
#![cfg(unix)]

mod common;

use common::*;
use std::fs;
use std::path::{Path, PathBuf};

const CHAT_SESSION: &str = "22222222-2222-4222-8222-222222222222";

fn churn_claude(home: &Path, minute: u32) -> String {
    let chat = home
        .join(".claude")
        .join("projects")
        .join("proj-beta")
        .join(format!("{CHAT_SESSION}.jsonl"));
    fs::create_dir_all(chat.parent().unwrap()).unwrap();
    let text = format!("claude churn {minute}");
    let row = serde_json::json!({
        "type": "user", "userType": "external", "sessionId": CHAT_SESSION,
        "timestamp": format!("2026-01-03T10:{minute:02}:00.000Z"), "cwd": "/work/beta",
        "message": {"role": "user", "content": text},
    });
    let mut body = fs::read_to_string(&chat).unwrap_or_default();
    body.push_str(&format!("{row}\n"));
    fs::write(&chat, body).unwrap();
    text
}

/// Two roots of one adapter: `(kept root, removed root, kept text, removed text)`.
fn two_roots(adapter: &str, home: &Path) -> (PathBuf, PathBuf, &'static str, &'static str) {
    copy_dir(&fixture_home("claude"), home);
    match adapter {
        "cline" => {
            let kept = home.join(".cline").join("data");
            let removed = join_native(
                home,
                ".config/Code/User/globalStorage/saoudrizwan.claude-dev",
            );
            cline_task(&kept, "1767348000000", "alpha task in the cli root");
            cline_task(&removed, "1767348900000", "beta task in the vscode root");
            (
                kept,
                removed,
                "alpha task in the cli root",
                "beta task in the vscode root",
            )
        }
        "crush" => {
            let kept = join_native(home, ".local/share/crush");
            let removed = home.join(".crush");
            crush_db(&kept.join("crush.db"), "sa", "alpha chat in the share root");
            crush_db(&removed.join("crush.db"), "sb", "beta chat in the dot root");
            (
                kept,
                removed,
                "alpha chat in the share root",
                "beta chat in the dot root",
            )
        }
        _ => unreachable!(),
    }
}

#[derive(Clone, Copy, Debug)]
enum Lane {
    Ordinary,
    CacheLost,
    Pending,
    EventRepair,
    Full,
}

fn prepare(data: &Path, lane: Lane) {
    match lane {
        Lane::Ordinary | Lane::Full => {}
        Lane::CacheLost => {
            fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
            let _ = fs::remove_file(data.join(".ingest_cache.bin.journal"));
        }
        Lane::Pending => {
            fs::copy(
                data.join(".source_snapshot.bin"),
                data.join(".ingest_pending.bin"),
            )
            .unwrap();
        }
        Lane::EventRepair => {
            for entry in fs::read_dir(data).unwrap().flatten() {
                if entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".events_complete")
                {
                    fs::remove_file(entry.path()).unwrap();
                }
            }
        }
    }
}

fn cleanup(home: &Path, data: &Path) {
    let _ = fs::remove_dir_all(home);
    let _ = fs::remove_dir_all(data);
}

/// One root removed for good: within three churned passes of any lane its rows are gone, the
/// sibling's rows and the churn stay published, and no pass wedges on a pending marker.
#[test]
fn removing_one_root_beside_a_live_sibling_converges_in_every_lane() {
    for adapter in ["cline", "crush"] {
        for lane in [
            Lane::Ordinary,
            Lane::CacheLost,
            Lane::Pending,
            Lane::EventRepair,
            Lane::Full,
        ] {
            let tag = format!("{adapter} {lane:?}");
            let home = temp_dir("multi-root-home");
            let data = temp_dir("multi-root-data");
            let (_, removed, kept_text, removed_text) = two_roots(adapter, &home);
            churn_claude(&home, 0);
            ingest_into("all", &home, &data, false);
            let published = normalize(&data);
            assert!(
                published.contains(kept_text) && published.contains(removed_text),
                "{tag}: both roots did not publish"
            );
            fs::remove_dir_all(&removed).unwrap();
            prepare(&data, lane);
            let mut converged = false;
            for minute in 1..=3 {
                let churn = churn_claude(&home, minute);
                let full = matches!(lane, Lane::Full) && minute == 1;
                let output = ingest_output("all", &home, &data, full);
                let stderr = String::from_utf8_lossy(&output.stderr);
                assert!(
                    output.status.success(),
                    "{tag} pass {minute} refused:\n{stderr}"
                );
                let published = normalize(&data);
                assert!(
                    published.contains(kept_text) && published.contains(&churn),
                    "{tag} pass {minute}: the sibling or the churn went missing"
                );
                converged |= !published.contains(removed_text);
            }
            assert!(converged, "{tag}: the removed root's rows never converged");
            assert!(
                !data.join(".ingest_pending.bin").exists(),
                "{tag}: a pending marker outlived convergence"
            );
            cleanup(&home, &data);
        }
    }
}

/// A root away for one pass and back for the next: its rows return with it, reparsed, and no
/// pass refuses.
#[test]
fn a_root_away_for_one_pass_returns_with_its_rows() {
    for adapter in ["cline", "crush"] {
        let home = temp_dir("multi-root-away-home");
        let data = temp_dir("multi-root-away-data");
        let (_, removed, kept_text, removed_text) = two_roots(adapter, &home);
        churn_claude(&home, 0);
        ingest_into("all", &home, &data, false);
        let away = removed.with_extension("away");
        fs::rename(&removed, &away).unwrap();
        let churn = churn_claude(&home, 1);
        ingest_into("all", &home, &data, false);
        let published = normalize(&data);
        assert!(
            published.contains(kept_text) && published.contains(&churn),
            "{adapter}: the sibling or the churn went missing while the root was away"
        );
        fs::rename(&away, &removed).unwrap();
        let churn = churn_claude(&home, 2);
        ingest_into("all", &home, &data, false);
        let published = normalize(&data);
        assert!(
            published.contains(removed_text) && published.contains(&churn),
            "{adapter}: the returned root's rows did not come back"
        );
        cleanup(&home, &data);
    }
}
