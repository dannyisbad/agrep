use serde_json::Value;
use std::{fs, path::Path, process::Command};
use tempfile::TempDir;
mod support;

fn run(home: &Path, data: &Path, args: &[&str]) -> std::process::Output {
    run_profile(home, data, args, "classic")
}

fn run_profile(home: &Path, data: &Path, args: &[&str], profile: &str) -> std::process::Output {
    Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_PROFILE", profile)
        .args(args)
        .output()
        .unwrap()
}

fn identities(output: &[u8]) -> Vec<String> {
    let mut rows: Vec<_> = String::from_utf8_lossy(output)
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|row| row.get("handle").is_some())
        .map(|row| serde_json::json!([row["who"], row["snippet"]]).to_string())
        .collect();
    rows.sort();
    rows
}

#[test]
fn unicode_keyword_and_word_rows_match_0_4_9() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/unicode_0_4_9");
    let relative = ".pi/agent/sessions/project/unicode.jsonl";
    fs::create_dir_all(home.join(relative).parent().unwrap()).unwrap();
    fs::copy(fixture.join("home").join(relative), home.join(relative)).unwrap();
    let indexed = run(&home, &data, &["index"]);
    assert!(
        indexed.status.success(),
        "{}",
        String::from_utf8_lossy(&indexed.stderr)
    );
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(fixture.join("expected.json")).unwrap()).unwrap();
    assert_eq!(cases.len(), 62);
    for case in cases {
        let mut args: Vec<_> = case["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        args.extend(["--json", "--no-auto"]);
        let output = run(&home, &data, &args);
        let mut expected: Vec<_> = case["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| serde_json::json!([row["who"], row["snippet"]]).to_string())
            .collect();
        expected.sort();
        assert_eq!(
            output.status.code(),
            Some(if expected.is_empty() { 2 } else { 0 }),
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            identities(&output.stdout),
            expected,
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        if args[0].contains('\u{307}') {
            let mut count_args = vec![args[0], "-c", "--no-auto"];
            if args.contains(&"-w") {
                count_args.push("-w");
            }
            let counted = run(&home, &data, &count_args);
            assert_eq!(
                String::from_utf8(counted.stdout)
                    .unwrap()
                    .trim()
                    .parse::<usize>()
                    .unwrap(),
                expected.len(),
                "{count_args:?}"
            );
        }
    }
}

#[test]
fn unicode_content_fallback_matches_0_4_9_lowercase_literals() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/unicode_0_4_9");
    let relative = ".pi/agent/sessions/project/unicode.jsonl";
    fs::create_dir_all(home.join(relative).parent().unwrap()).unwrap();
    fs::copy(fixture.join("home").join(relative), home.join(relative)).unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(fixture.join("fallback.json")).unwrap()).unwrap();
    assert_eq!(cases.len(), 10);
    for case in cases {
        let mut args: Vec<_> = case["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        args.push("--no-auto");
        let output = run_profile(&home, &data, &args, "compact");
        let mut actual: Vec<_> = String::from_utf8_lossy(&output.stdout)
            .lines()
            .filter(|line| line.starts_with('@'))
            .map(|line| {
                let fields: Vec<_> = line.splitn(6, ' ').collect();
                assert_eq!(fields[4], "~all", "{line}");
                serde_json::json!([fields[1].strip_prefix("pi/").unwrap(), fields[5]]).to_string()
            })
            .collect();
        let mut expected: Vec<_> = case["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| serde_json::json!([row["who"], row["snippet"]]).to_string())
            .collect();
        actual.sort();
        expected.sort();
        assert_eq!(
            actual,
            expected,
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

#[test]
fn unicode_postings_follow_rewrites_and_deletions() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let fresh = temp.path().join("fresh");
    let source = home.join(".pi/agent/sessions/project/unicode.jsonl");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/unicode_0_4_9/home/.pi/agent/sessions/project/unicode.jsonl");
    fs::copy(fixture, &source).unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    let rewritten = fs::read_to_string(&source)
        .unwrap()
        .lines()
        .enumerate()
        .map(|(i, line)| {
            let mut row: Value = serde_json::from_str(line).unwrap();
            if let Some(message) = row.get_mut("message") {
                let field = if message["role"] == "user" {
                    "content"
                } else {
                    "output"
                };
                message[field] = Value::String(if i % 4 < 2 {
                    "cafe\u{301}".into()
                } else {
                    "ＫＩＴ".into()
                });
            }
            row.to_string() + "\n"
        })
        .collect::<String>();
    fs::write(&source, rewritten).unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    assert!(run(&home, &fresh, &["index"]).status.success());
    for pattern in ["cafe", "kit", "ＫＩＴ", "office", "..."] {
        for mode in ["--exact", "-w"] {
            let args = [pattern, mode, "-n", "0", "--json", "--no-auto"];
            let incremental = run(&home, &data, &args);
            let rebuilt = run(&home, &fresh, &args);
            assert_eq!(incremental.status.code(), rebuilt.status.code(), "{args:?}");
            assert_eq!(
                identities(&incremental.stdout),
                identities(&rebuilt.stdout),
                "{args:?}"
            );
        }
    }
    fs::remove_file(source).unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    for pattern in ["cafe", "ＫＩＴ"] {
        let result = run(&home, &data, &[pattern, "--json", "--no-auto"]);
        assert!(identities(&result.stdout).is_empty());
    }
}

#[test]
fn mixed_literal_and_expanded_dots_keep_complete_candidates() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let oracle: Value = serde_json::from_str(include_str!(
        "fixtures/unicode_0_4_9/lowercase_combinations.json"
    ))
    .unwrap();
    let source = home.join(".pi/agent/sessions/project/mixed.jsonl");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    let mut lines = vec![serde_json::json!({"type":"session","id":"mixed-dot","version":3,"cwd":"/work/project","timestamp":"2099-01-01T00:00:00Z"}).to_string()];
    for (i, text) in oracle["texts"].as_array().unwrap().iter().enumerate() {
        lines.push(serde_json::json!({"type":"message","id":format!("m{i}"),"parentId":if i==0 {Value::Null} else {Value::String(format!("m{}",i-1))},"timestamp":"2099-01-01T00:00:01Z","message":{"role":"user","content":text}}).to_string());
    }
    fs::write(source, lines.join("\n") + "\n").unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    for case in oracle["cases"].as_array().unwrap() {
        let mut args = vec![
            oracle["pattern"].as_str().unwrap(),
            "-n",
            "0",
            "--json",
            "--no-auto",
        ];
        if case["word"] == true {
            args.push("-w");
        }
        let output = run(&home, &data, &args);
        assert_eq!(output.status.code(), Some(0));
        let mut expected: Vec<_> = case["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| serde_json::json!([row["who"], row["snippet"]]).to_string())
            .collect();
        expected.sort();
        assert_eq!(identities(&output.stdout), expected, "{args:?}");
    }
}

#[test]
fn lowercase_occurrences_precede_raw_case_aliases() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let oracle: Value =
        serde_json::from_str(include_str!("fixtures/unicode_0_4_9/priority.json")).unwrap();
    let source = home.join(".pi/agent/sessions/project/priority.jsonl");
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    let mut lines = vec![serde_json::json!({"type":"session","id":"priority-session","version":3,"cwd":"/work/project","timestamp":"2099-01-01T01:00:00Z"}).to_string()];
    for (i, text) in oracle["texts"].as_array().unwrap().iter().enumerate() {
        for tool in [false, true] {
            let n = i * 2 + usize::from(tool);
            let message = if tool {
                serde_json::json!({"role":"bashExecution","command":"printf unicode","output":text,"exitCode":0,"cancelled":false,"truncated":false})
            } else {
                serde_json::json!({"role":"user","content":text})
            };
            lines.push(serde_json::json!({"type":"message","id":format!("m{n}"),"parentId":if n==0 {Value::Null} else {Value::String(format!("m{}",n-1))},"timestamp":format!("2099-01-01T01:{i:02}:00Z"),"message":message}).to_string());
        }
    }
    fs::write(&source, lines.join("\n") + "\n").unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    for case in oracle["cases"].as_array().unwrap() {
        let pattern = case["args"][0].as_str().unwrap();
        let output = run(&home, &data, &[pattern, "-n", "0", "--json", "--no-auto"]);
        let mut expected: Vec<_> = case["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| serde_json::json!([row["who"], row["snippet"]]).to_string())
            .collect();
        expected.sort();
        assert_eq!(identities(&output.stdout), expected, "{pattern}");
        let counted = run(&home, &data, &[pattern, "-c", "--no-auto"]);
        assert_eq!(
            String::from_utf8(counted.stdout)
                .unwrap()
                .trim()
                .parse::<usize>()
                .unwrap(),
            case["count"].as_u64().unwrap() as usize,
            "{pattern}"
        );
    }
    // CİTY kit was rejected by the oracle; removing it must preserve the count.
    let body = fs::read_to_string(&source).unwrap();
    fs::write(&source, body.replace("CİTY kit", "unrelated")).unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    let count = || {
        let output = run(&home, &data, &["cities kit", "-c", "--no-auto"]);
        String::from_utf8(output.stdout)
            .unwrap()
            .trim()
            .parse::<u64>()
            .unwrap()
    };
    let expected = oracle["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["args"][0] == "cities kit")
        .unwrap()["count"]
        .as_u64()
        .unwrap();
    assert_eq!(count(), expected);
    fs::remove_file(source).unwrap();
    assert!(run(&home, &data, &["index"]).status.success());
    assert_eq!(count(), 0);
}
