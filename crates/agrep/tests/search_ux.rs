//! Search, chats, summary and continuation-page behavior agrep 0.4.9 users relied on.
mod support;
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};
use tempfile::TempDir;

struct Home {
    _t: TempDir,
    h: PathBuf,
    d: PathBuf,
}
impl Home {
    /// pi sessions, each a project directory and its user turns; every turn gets a reply.
    fn new(sessions: &[(&str, &[&str])]) -> Self {
        let t = TempDir::new().unwrap();
        let (h, d) = (t.path().join("h"), t.path().join("d"));
        for (n, (cwd, turns)) in sessions.iter().enumerate() {
            let id = format!("0dd{n:05}-0000-7000-8000-000000000001");
            let dir = h.join(".pi/agent/sessions").join(format!("s{n}"));
            fs::create_dir_all(&dir).unwrap();
            let mut lines = vec![
                json!({"type":"session","id":id,"version":3,"cwd":cwd,"timestamp":"2026-10-04T12:00:00.000Z"}),
            ];
            let mut parent: Option<String> = None;
            for (i, text) in turns.iter().enumerate() {
                for (role, body, at) in [("user", *text, 2 * i), ("assistant", "noted", 2 * i + 1)]
                {
                    let row = format!("r{i}{role}");
                    lines.push(json!({"type":"message","id":row,"parentId":parent,"timestamp":format!("2026-10-04T12:{:02}:{:02}.000Z", at / 60, at % 60),"message":{"role":role,"model":"m","provider":"p","content":[{"type":"text","text":body}]}}));
                    parent = Some(row);
                }
            }
            fs::write(
                dir.join(format!("2026-10-04T12-00-00-000Z_{id}.jsonl")),
                lines
                    .iter()
                    .map(Value::to_string)
                    .collect::<Vec<_>>()
                    .join("\n")
                    + "\n",
            )
            .unwrap();
        }
        let home = Self { _t: t, h, d };
        let index = home.run(&["index"], "classic");
        assert!(index.status.success(), "{}", stderr(&index));
        home
    }
    fn run(&self, args: &[&str], profile: &str) -> Output {
        command(&self.h, &self.d, args, profile).output().unwrap()
    }
}
fn command(h: &Path, d: &Path, args: &[&str], profile: &str) -> Command {
    let mut cmd = Command::new(support::binary());
    cmd.env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", h)
        .env("AGREP_HOME", h)
        .env("AGREP_DATA_DIR", d)
        .env("AGREP_CALLER_PUBLICATION_DIR", h.join("empty-callers"))
        .env("AGREP_PROFILE", profile)
        .args(args);
    cmd
}
fn stdout(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).into_owned()
}
fn stderr(o: &Output) -> String {
    String::from_utf8_lossy(&o.stderr).into_owned()
}

// 0.4.9's `project_label_matches` (py/surface_policy.py:720-729): a glob matches a chat's
// project label or the label's last path segment, on every surface.
#[test]
fn project_globs_match_the_label_or_its_last_segment() {
    let home = Home::new(&[
        ("/work/agrep-private", &["zebrafish in the main repo"]),
        ("/work/other", &["zebrafish elsewhere", "zebrafish again"]),
    ]);
    for (args, expected) in [
        (vec!["zebrafish", "-c", "--project", "agrep*"], "1\n"),
        (vec!["zebrafish", "-c", "--project", "agrep-p?ivate"], "1\n"),
        (
            vec!["zebrafish", "-c", "--exclude-project", "agrep*"],
            "2\n",
        ),
    ] {
        let o = home.run(&args, "classic");
        assert_eq!(stdout(&o), expected, "{args:?}: {}", stderr(&o));
    }
    let recall = home.run(
        &["recall", "zebrafish", "--lexical", "--project", "agrep*"],
        "classic",
    );
    assert!(recall.status.success(), "{}", stderr(&recall));
    assert!(stdout(&recall).contains("main repo"), "{}", stdout(&recall));
    let chats = home.run(
        &["chats", "zebrafish", "--project", "agrep*", "--json"],
        "classic",
    );
    let rows: Vec<Value> = stdout(&chats)
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .filter(|v: &Value| v["kind"] != "agrep-meta")
        .collect();
    assert_eq!(rows.len(), 1, "{}", stdout(&chats));
    assert_eq!(rows[0]["project"], "/work/agrep-private");
}

// 0.4.9 previewed a chat's content match by the match's snippet (py/search.py:8875-8876).
#[test]
fn chat_previews_show_the_match() {
    let turn = format!("{} then the zebrafish arrived", "filler words ".repeat(20));
    let home = Home::new(&[("/work/fish", &[turn.as_str()])]);
    let text = home.run(&["chats", "zebrafish"], "classic");
    assert!(
        stdout(&text).contains("match: …") && stdout(&text).contains("zebrafish"),
        "{}",
        stdout(&text)
    );
    let json = home.run(&["chats", "zebrafish", "--json"], "classic");
    let row: Value = serde_json::from_str(stdout(&json).lines().nth(1).unwrap()).unwrap();
    let preview = row["match_text"].as_str().unwrap();
    assert!(
        preview.starts_with('…') && preview.contains("zebrafish arrived"),
        "{preview}"
    );
}

// The last compact page of a cut search names the larger `-n` rerun (PORTING.md).
#[test]
fn compact_pages_end_with_the_larger_rerun() {
    let turns: Vec<String> = (0..9).map(|i| format!("zebrafish note {i}")).collect();
    let turns: Vec<&str> = turns.iter().map(String::as_str).collect();
    let sessions: Vec<(String, &[&str])> = (0..6)
        .map(|n| (format!("/work/p{n}"), turns.as_slice()))
        .collect();
    let sessions: Vec<(&str, &[&str])> = sessions.iter().map(|(p, t)| (p.as_str(), *t)).collect();
    let home = Home::new(&sessions);
    let rerun = "larger page: agrep --classic -n 80 -- zebrafish";
    let short = home.run(&["zebrafish", "-n", "5"], "compact");
    assert_eq!(stdout(&short).lines().count(), 5);
    assert_eq!(
        stderr(&short).trim_end(),
        format!("54+ matches (floor; -c exact) · {rerun}"),
        "{}",
        stderr(&short)
    );
    let mut page = home.run(&["zebrafish"], "compact");
    let mut shown = stdout(&page).lines().count();
    loop {
        let footer = stderr(&page);
        let Some(handle) = footer.split("agrep --more ").nth(1) else {
            assert_eq!(
                footer.trim_end(),
                format!("54+ matches (floor; -c exact) · {rerun}")
            );
            break;
        };
        page = home.run(&["--more", handle.trim()], "compact");
        assert!(page.status.success(), "{}", stderr(&page));
        shown += stdout(&page).lines().count();
    }
    assert_eq!(shown, 40);
}

// 0.4.9 answered a pattern without a word with no match, and searched a one-word pattern
// for its whole text (py/corpusdb.py:4731-4758, 4817-4828).
#[test]
fn wordless_and_decorated_patterns_search_as_0_4_9() {
    let home = Home::new(&[(
        "/work/flags",
        &["pass --verbose to the tool", "verbose output again"],
    )]);
    for (args, expected, code) in [
        (vec!["_", "-c"], "0\n", 1),
        (vec!["-c", "--", "-"], "0\n", 1),
        (vec!["-c", "--", "--verbose"], "1\n", 0),
        (vec!["-c", "verbose"], "2\n", 0),
        (vec!["-c", "--", "--x"], "0\n", 1),
    ] {
        let o = home.run(&args, "classic");
        assert_eq!(
            (stdout(&o).as_str(), o.status.code()),
            (expected, Some(code)),
            "{args:?}: {}",
            stderr(&o)
        );
    }
    let miss = home.run(&["_"], "classic");
    assert_eq!(miss.status.code(), Some(1), "{}", stderr(&miss));
    assert_eq!(
        stderr(&miss),
        "keyword: 0 matching rows across 2 indexed messages\n"
    );
}

#[test]
fn summary_names_a_filter_value_missing_from_the_index() {
    let home = Home::new(&[("/work/fish", &["zebrafish"])]);
    let o = home.run(&["summary", "--project", "nonexistent"], "classic");
    assert_eq!(o.status.code(), Some(1));
    assert!(
        stderr(&o)
            .contains("--project nonexistent isn't in the index - indexed projects: /work/fish"),
        "{}",
        stderr(&o)
    );
}

// `-l -c` is refused, so a chat listing's floor does not point at `-c`.
#[test]
fn chat_listing_floor_names_no_refused_count() {
    let home = Home::new(&[
        ("/work/a", &["zebrafish one"]),
        ("/work/b", &["zebrafish two"]),
    ]);
    let o = home.run(&["zebrafish", "-l", "-n", "1"], "classic");
    assert!(o.status.success(), "{}", stderr(&o));
    assert_eq!(
        stderr(&o),
        "showing 1 of 2+ chats (a floor, not the total) · larger page: agrep -l -n 80 -- zebrafish\n"
    );
}
