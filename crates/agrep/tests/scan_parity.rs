//! Indexed plans against a full scan. An `-E` expression behind an empty lookahead
//! leaves no literal for any index to use, so the same verifier reads every row; each
//! planned search must return exactly what that scan returns.
use serde_json::{json, Value};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
use tempfile::TempDir;
mod support;

/// 2100-01-01, so both searches of a pair rank rows at the same instant.
const NOW_MS: &str = "4102444800000";
const JAPANESE: &str =
    "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。何でも薄暗いじめじめした所で";

fn run(home: &Path, data: &Path, args: &[&str]) -> Output {
    Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_PROFILE", "classic")
        .env("AGREP_NOW_MS", NOW_MS)
        .args(args)
        .output()
        .unwrap()
}

/// Writes one pi session holding each text as a user row and as a tool row's output.
fn write_session(home: &Path, texts: &[String]) {
    let source = home.join(".pi/agent/sessions/project/parity.jsonl");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    let mut lines = vec![json!({"type":"session","id":"parity-session","version":3,"cwd":"/work/project","timestamp":"2099-01-01T00:00:00Z"}).to_string()];
    for (i, text) in texts.iter().enumerate() {
        for tool in [false, true] {
            let n = i * 2 + usize::from(tool);
            let message = if tool {
                json!({"role":"bashExecution","command":"printf parity","output":text,"exitCode":0,"cancelled":false,"truncated":false})
            } else {
                json!({"role":"user","content":text})
            };
            let timestamp = format!(
                "2099-01-{:02}T{:02}:{:02}:00Z",
                1 + n / 1440,
                n / 60 % 24,
                n % 60
            );
            let parent = if n == 0 {
                Value::Null
            } else {
                Value::String(format!("m{}", n - 1))
            };
            lines.push(json!({"type":"message","id":format!("m{n}"),"parentId":parent,"timestamp":timestamp,"message":message}).to_string());
        }
    }
    fs::write(source, lines.join("\n") + "\n").unwrap();
}

fn index_into(home: &Path, data: &Path) {
    let indexed = run(home, data, &["index"]);
    assert!(
        indexed.status.success(),
        "{}",
        String::from_utf8_lossy(&indexed.stderr)
    );
}

/// A home holding `texts` (see `write_session`), indexed into its `data`.
fn index(texts: &[String]) -> TempDir {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    write_session(&home, texts);
    index_into(&home, &temp.path().join("data"));
    temp
}

/// What a search printed, with JSON rows reduced to their speaker and snippet. A page's
/// total depends on the blocks its bounds let it visit, so only an exact total counts.
fn shown(output: &Output) -> (Option<i32>, Vec<String>) {
    let text = String::from_utf8_lossy(&output.stdout);
    let rows = text
        .lines()
        .filter_map(|line| match serde_json::from_str::<Value>(line) {
            Ok(row) if row.get("handle").is_some() => {
                Some(json!([row["who"], row["snippet"]]).to_string())
            }
            Ok(row) => (row["completeness"]["total_basis"] == "exact")
                .then(|| row["completeness"]["total"].to_string()),
            Err(_) => Some(line.to_owned()),
        })
        .collect();
    (output.status.code(), rows)
}

const MODES: [&[&str]; 5] = [
    &["-c"],
    &["--who", "tool", "-c"],
    &["-n", "0", "--json"],
    &["-n", "5", "--json"],
    &["--sort", "time", "-n", "5", "--json"],
];

/// Each search of `cases` in every mode prints what its full scan prints, and finds a row.
fn assert_scan_parity(temp: &TempDir, cases: &[(Vec<&str>, String)]) {
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    for (planned, scan) in cases {
        for mode in MODES {
            let mut args = planned.clone();
            args.extend(mode);
            args.push("--no-auto");
            let mut scan_args = vec!["-E", scan.as_str()];
            scan_args.extend(mode);
            scan_args.push("--no-auto");
            let expected = shown(&run(&home, &data, &scan_args));
            let actual = shown(&run(&home, &data, &args));
            if mode.contains(&"-c") {
                assert_eq!(actual, expected, "{args:?}");
                assert_ne!(expected.1, ["0"], "{args:?} finds nothing");
            } else {
                // A page's total is a floor wherever its bounds stop it early.
                assert_eq!(actual.0, expected.0, "{args:?}");
                let hits = |rows: &[String]| -> Vec<String> {
                    rows.iter()
                        .filter(|r| r.starts_with('['))
                        .cloned()
                        .collect()
                };
                assert_eq!(hits(&actual.1), hits(&expected.1), "{args:?}");
                if mode.contains(&"0") {
                    assert_eq!(actual.1, expected.1, "{args:?}");
                }
            }
        }
    }
}

fn texts(base: &[String]) -> Vec<String> {
    (0..12)
        .flat_map(|copy| base.iter().map(move |text| format!("{text} {copy}")))
        .collect()
}

// The extractor cuts a literal at 100 bytes, inside a character here; the cut literal
// once made an `-E` search of a long non-ASCII literal find nothing.
#[test]
fn long_non_ascii_literals_match_a_full_scan() {
    let accented = format!("a{}b", "É".repeat(60));
    let emoji = format!("a{}b", "🚀".repeat(30));
    let base: Vec<String> = [
        JAPANESE.to_owned(),
        format!("前置き{JAPANESE}"),
        JAPANESE.chars().take(30).collect(),
        accented.clone(),
        accented.to_lowercase(),
        format!("a{}b", "é".repeat(59)),
        emoji.clone(),
        format!("a{}b", "🚀".repeat(29)),
        "unrelated text".into(),
    ]
    .into();
    let temp = index(&texts(&base));
    let cases: Vec<_> = [JAPANESE, &accented, &emoji]
        .into_iter()
        .map(|literal| (vec!["-E", literal], format!("(?=){literal}")))
        .collect();
    assert_scan_parity(&temp, &cases);
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let count = |args: &[&str]| String::from_utf8(run(&home, &data, args).stdout).unwrap();
    assert_eq!(count(&["-E", JAPANESE, "-c", "--no-auto"]), "48\n");
    assert_eq!(count(&[JAPANESE, "-c", "--no-auto"]), "48\n");
}

#[test]
fn alternations_and_whole_words_match_a_full_scan() {
    let base: Vec<String> = [
        "release the workflow",
        "Workflows ran",
        "WORKTREE pruned",
        "worktrees listed",
        "work tree split",
        "network flow",
        "homework due",
        "work",
        "work_tree",
        "work-tree",
        "work2 x",
        "éwork",
        "work日本",
        "🚀work🚀",
        "(work)",
        "WORK.",
        "rework",
        "colour",
        "color",
        "colr",
        "foobar",
        "bar",
        "baz",
    ]
    .map(String::from)
    .into();
    let temp = index(&texts(&base));
    let mut cases: Vec<_> = [
        "release|workflow",
        "work(tree|flow)s?",
        "Work(TREE|flow)",
        "colou?r",
        "(foo)?bar",
    ]
    .into_iter()
    .map(|pattern| (vec!["-E", pattern], format!("(?=){pattern}")))
    .collect();
    for term in ["work", "WORK", "tree", "flow", "bar"] {
        cases.push((vec!["-w", term], word(term)));
    }
    assert_scan_parity(&temp, &cases);
}

fn word(term: &str) -> String {
    format!(
        "(?=)(?<![\\p{{L}}\\p{{N}}_]){}(?![\\p{{L}}\\p{{N}}_])",
        regex::escape(term)
    )
}

/// The rows a search lists, by speaker and row (a tool row's handle also names the span
/// its match took, which a keyword and an expression place differently).
fn listed(output: &Output) -> Vec<String> {
    let mut rows: Vec<String> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter_map(|row| {
            let handle = row["handle"].as_str()?.split('~').next()?.to_owned();
            Some(json!([row["who"], handle]).to_string())
        })
        .collect();
    rows.sort();
    rows
}

// Punctuation, emoji and other scripts are no dictionary words; the symbol index admits
// the rows holding them, and every plan must still find exactly the scan's rows.
#[test]
fn symbol_terms_match_a_full_scan() {
    let base: Vec<String> = [
        "built libc++ and c++20 today",
        "C + + spaced",
        "c++",
        "the c# and f# langs",
        "notc++x",
        "rocket 🚀 launch",
        "🚀🚀 double",
        "rocket🚀glued",
        "O(n log n) sort",
        "o(n) linear",
        "n log n plain",
        "(n) alone",
        "日本語のテキスト",
        "日本",
        "中文字符",
        "café crème",
        "CAFÉ",
        "cafe plain",
        "naïve façade",
        "say \"quoted\" here",
        "'single' quotes",
        "\"q\"",
        "std::sync::Arc",
        "a::b",
        ": : spaced",
        "path :: alone",
        "*.rs files",
        "src/**/*.rs",
        "glob ?(x)",
        "nothing special",
    ]
    .map(String::from)
    .into();
    let temp = index(&texts(&base));
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    for pattern in [
        "C++",
        "c#",
        "🚀",
        "🚀🚀",
        "O(n log n)",
        "o(n)",
        "日本",
        "テキスト",
        "café",
        "\"quoted\"",
        "'single'",
        "std::sync",
        "::",
        "*.rs",
        "src/**/*.rs",
        "?(x)",
        "a",
    ] {
        let scan = format!(
            "(?s)(?=){}",
            pattern
                .split(' ')
                .map(|token| format!("(?=.*{})", regex::escape(token)))
                .collect::<String>()
        );
        for mode in [&["-c"][..], &["--who", "tool", "-c"]] {
            let mut args = vec![pattern];
            args.extend(mode);
            args.push("--no-auto");
            let mut scan_args = vec!["-E", scan.as_str()];
            scan_args.extend(mode);
            scan_args.push("--no-auto");
            let expected = shown(&run(&home, &data, &scan_args));
            assert_eq!(shown(&run(&home, &data, &args)), expected, "{args:?}");
            assert_ne!(expected.1, ["0"], "{args:?} finds nothing");
        }
        let listing = |args: &[&str]| listed(&run(&home, &data, args));
        assert_eq!(
            listing(&[pattern, "-n", "0", "--json", "--no-auto"]),
            listing(&["-E", &scan, "-n", "0", "--json", "--no-auto"]),
            "{pattern}"
        );
    }
    let mut cases: Vec<_> = [
        "c\\+\\+",
        "🚀+",
        "o\\(n( log n)?\\)",
        "日本|中文",
        "caf(é|e)",
        "std::(sync|io)",
        "\\*\\.rs",
        "\"q",
    ]
    .into_iter()
    .map(|pattern| (vec!["-E", pattern], format!("(?=){pattern}")))
    .collect();
    for term in ["c++", "C#", "🚀", "::"] {
        cases.push((vec!["-w", term], word(term)));
    }
    assert_scan_parity(&temp, &cases);
}

// A refresh keeps the symbol index of appended, rewritten and removed rows: each
// state's searches equal a first build's of the same transcripts.
#[test]
fn symbol_rows_follow_appends_rewrites_and_removals() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let first: Vec<String> = ["libc++ 🚀", "std::sync", "o(n) plain", "nothing"]
        .map(String::from)
        .into();
    write_session(&home, &first);
    index_into(&home, &data);
    let appended: Vec<String> = first
        .iter()
        .cloned()
        .chain(["more c++ here", "🚀🚀 and ::", "x"].map(String::from))
        .collect();
    let rewritten: Vec<String> = ["plain words", "c++ only", "o(n) and 🚀", "a::b", "x"]
        .map(String::from)
        .into();
    for (state, texts) in [("append", appended), ("rewrite", rewritten)] {
        write_session(&home, &texts);
        index_into(&home, &data);
        let fresh = temp.path().join(format!("fresh-{state}"));
        index_into(&home, &fresh);
        for pattern in ["C++", "🚀", "::", "o(n)", "🚀🚀"] {
            for mode in [&["-c"][..], &["-n", "0", "--json"]] {
                let mut args = vec![pattern];
                args.extend(mode);
                args.push("--no-auto");
                assert_eq!(
                    shown(&run(&home, &data, &args)),
                    shown(&run(&home, &fresh, &args)),
                    "{state} {args:?}"
                );
            }
        }
    }
    fs::remove_file(home.join(".pi/agent/sessions/project/parity.jsonl")).unwrap();
    index_into(&home, &data);
    for pattern in ["C++", "🚀", "::"] {
        let counted = run(&home, &data, &[pattern, "-c", "--no-auto"]);
        assert_eq!(String::from_utf8_lossy(&counted.stdout), "0\n", "{pattern}");
    }
}

// A root the previous format wrote (its index has no symbol index) is refused, naming the
// command that rebuilds it, and `reindex` rebuilds it in place: its searches then equal a
// full scan, and its settings stay.
#[test]
fn reindex_rebuilds_a_previous_format_root_in_place() {
    let base: Vec<String> = [
        "built libc++ today",
        "rocket 🚀 launch",
        "std::sync",
        "nothing",
    ]
    .map(String::from)
    .into();
    let temp = index(&texts(&base));
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    assert!(run(&home, &data, &["set", "embeddings", "off"])
        .status
        .success());
    rusqlite::Connection::open(data.join("index.db"))
        .unwrap()
        .execute_batch("DROP TABLE tools_sym")
        .unwrap();
    fs::write(data.join("FORMAT"), "agrep-data 8\n").unwrap();
    for args in [&["status"][..], &["C++", "-c"], &["index"]] {
        let refused = run(&home, &data, args);
        assert_eq!(refused.status.code(), Some(2), "{args:?}");
        assert!(refused.stdout.is_empty(), "{args:?}");
        assert!(
            String::from_utf8_lossy(&refused.stderr)
                .contains(" is from an earlier agrep build: run agrep reindex to rebuild it"),
            "{args:?}"
        );
    }
    let doctor = String::from_utf8(run(&home, &data, &["doctor"]).stdout).unwrap();
    assert!(
        doctor.contains(
            "index: failed - the index is from an earlier agrep build - `agrep reindex` rebuilds it"
        ),
        "{doctor}"
    );
    let rebuilt = run(&home, &data, &["reindex"]);
    assert!(
        rebuilt.status.success(),
        "{}",
        String::from_utf8_lossy(&rebuilt.stderr)
    );
    assert_eq!(fs::read(data.join("FORMAT")).unwrap(), b"agrep-data 9\n");
    let config: Value =
        serde_json::from_slice(&fs::read(data.join("config.json")).unwrap()).unwrap();
    assert_eq!(config["embeddings"], "off");
    for pattern in ["C++", "🚀", "::"] {
        let scan = format!("(?=){}", regex::escape(pattern));
        for mode in [&["-c"][..], &["--who", "tool", "-c"]] {
            let mut args = vec![pattern];
            args.extend(mode);
            args.push("--no-auto");
            let mut scan_args = vec!["-E", scan.as_str()];
            scan_args.extend(mode);
            scan_args.push("--no-auto");
            let expected = shown(&run(&home, &data, &scan_args));
            assert_eq!(shown(&run(&home, &data, &args)), expected, "{args:?}");
            assert_ne!(expected.1, ["0"], "{args:?} finds nothing");
        }
    }
}
