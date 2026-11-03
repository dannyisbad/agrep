use serde_json::{json, Value};
use std::{fs, path::Path, process::Command};

fn cli(home: &Path, args: &[&str]) -> std::process::Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_agrep"));
    command
        .env_clear()
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", home.join("data"))
        .env("AGREP_MODEL_DIR", home.join("absent-model"))
        .env("AGREP_CALLER_PUBLICATION_DIR", home.join("callers"));
    if let Some(root) = std::env::var_os("SystemRoot") {
        command.env("SystemRoot", root);
    }
    command.args(args).output().unwrap()
}
#[test]
fn every_json_surface_escapes_terminal_controls_and_preserves_values() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let path = home.join(".claude/projects/demo/root.jsonl");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let text = "needle del\u{7f} c1\u{85}\u{9b} bidi\u{202e}\u{2069} format\u{200b}\u{feff}";
    fs::write(path,format!("{}\n",json!({"type":"user","sessionId":"example","cwd":format!("/work/{text}"),"timestamp":chrono::Utc::now().to_rfc3339(),"message":{"role":"user","content":text}}))).unwrap();
    assert!(cli(home, &["set", "embeddings", "off"]).status.success());
    assert!(cli(home, &["index"]).status.success());
    let commands: &[&[&str]] = &[
        &["needle", "--json", "--lexical", "--no-auto"],
        &["chats", "--json", "--no-auto"],
        &["around", "example", "0", "--json", "--no-auto"],
        &["recall", "needle", "--json", "--lexical", "--no-auto"],
        &["pack", "needle", "--json", "--lexical", "--no-auto"],
        &["summary", "pending", "--json", "--no-auto"],
        &["postcompact", "--session", "example", "--json", "--no-auto"],
        &["why", "example", "--json", "--no-auto"],
        &["audit", "--json", "--no-auto"],
        &["status", "--json"],
        &["doctor", "--json"],
        &["board", "--json", "--wait", "5"],
        &["tail", "--snapshot"],
    ];
    for args in commands {
        let output = cli(home, args);
        let stdout = String::from_utf8(output.stdout).unwrap();
        assert!(
            !stdout.is_empty(),
            "{args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            !stdout
                .chars()
                .any(|c| matches!(c, '\u{7f}'..='\u{9f}')
                    || agrep_core::surface::is_terminal_format(c)),
            "raw terminal character in {args:?}"
        );
        let rows: Vec<Value> = stdout
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        if args[0] == "around" || args[0] == "tail" {
            assert!(
                rows.iter().any(|row| row.to_string().contains(text)),
                "lost decoded text in {args:?}: {rows:?}"
            );
        }
    }
}
