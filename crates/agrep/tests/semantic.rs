mod support;
use agrep_core::{data::DataRoot, semantic, store};
use std::{
    fs,
    process::{Command, Output},
};
fn run(t: &tempfile::TempDir, args: &[&str]) -> Output {
    Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path())
        .env("AGREP_HOME", t.path())
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"))
        .env("AGREP_PROFILE", "compact")
        .env("AGREP_DATA_READONLY", "1")
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn missing_model_is_one_line_exit_two_and_hybrid_stays_lexical() {
    let t = tempfile::tempdir().unwrap();
    let r = DataRoot::open(t.path().join("data"), true).unwrap();
    store::writer(&r).unwrap();
    let out = run(
        &t,
        &["--no-auto", "search", "-s", "database migration failure"],
    );
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&out.stderr),
        "semantic model not installed: run agrep setup\n"
    );
    assert!(out.stdout.is_empty());
    let out = run(&t, &["--no-auto", "search", "database migration failure"]);
    assert!(!String::from_utf8_lossy(&out.stderr).contains("semantic"));
    assert!(!t.path().join("model").exists());
}
#[test]
fn partial_semantic_zero_exits_one_and_reports_numbers() {
    let t = tempfile::tempdir().unwrap();
    let r = DataRoot::open(t.path().join("data"), true).unwrap();
    let c = store::writer(&r).unwrap();
    semantic::initialize(&r).unwrap();
    c.execute("INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra) VALUES(1,'codex','session','p',1,'user','',0,0,'text','digest','')",[]).unwrap();
    // Empty coverage never loads weights. Sparse file sizes exercise that path
    // without network, model initialization or embedding fabricated evidence.
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let out = run(
        &t,
        &[
            "--no-auto",
            "search",
            "-s",
            "--json",
            "database migration failure",
        ],
    );
    assert_eq!(
        out.status.code(),
        Some(1),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let meta: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(meta["semantic_coverage"]["indexed"], 0);
    assert_eq!(meta["semantic_coverage"]["total"], 1);
    let out = run(
        &t,
        &["--no-auto", "search", "-s", "database migration failure"],
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("semantic 0/1 rows"));
}

#[test]
fn optional_model_failure_keeps_a_completed_keyword_answer() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join(".pi/agent/sessions/fixture");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("case.jsonl"), concat!(
        "{\"type\":\"session\",\"id\":\"semantic-failure-fixture\",\"version\":3,\"cwd\":\"/fixture\",\"timestamp\":\"2026-10-29T10:00:00Z\"}\n",
        "{\"type\":\"message\",\"id\":\"m1\",\"parentId\":null,\"timestamp\":\"2026-10-29T10:01:00Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"database migration failure\"}]}}\n"
    )).unwrap();
    let out = Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path())
        .env("AGREP_HOME", t.path())
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"))
        .arg("index")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let r = DataRoot::open(t.path().join("data"), false).unwrap();
    semantic::initialize(&r).unwrap();
    store::writer(&r)
        .unwrap()
        .execute(
            "INSERT INTO semantic_refs SELECT id,digest,digest FROM prose",
            [],
        )
        .unwrap();
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let out = run(&t, &["--no-auto", "search", "database migration failure"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("database migration failure"));
    assert!(!String::from_utf8_lossy(&out.stderr).contains("semantic"));
}

#[test]
fn first_missing_model_request_has_only_the_setup_instruction() {
    let t = tempfile::tempdir().unwrap();
    let out = run(&t, &["search", "-s", "database migration failure"]);
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&out.stderr),
        "semantic model not installed: run agrep setup\n"
    );
    assert!(!t.path().join("data").exists());
}
