use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

const BIN: &str = env!("CARGO_BIN_EXE_agrep-rs");
static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TempRoot(PathBuf);

impl TempRoot {
    fn new(tag: &str) -> Self {
        let serial = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "agrep-goal9-readonly-{tag}-{}-{nanos}-{serial}",
            std::process::id()
        ));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempRoot {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn semantic_build(protected: &Path, output: &Path) -> Output {
    Command::new(BIN)
        .args([
            "semantic-q8-build",
            "--embeddings",
            "missing-embeddings.f32",
            "--meta",
            "missing-meta.json",
            "--groups",
            "missing-groups.jsonl",
            "--output-dir",
        ])
        .arg(output)
        .env("AGREP_DATA_READONLY", protected)
        .output()
        .expect("run semantic-q8-build")
}

fn hold(protected: &Path, lock: &Path) -> Output {
    Command::new(BIN)
        .args(["index-lock-contract", "hold", "--path"])
        .arg(lock)
        .args(["--label", "goal9-test", "--timeout-ms", "1"])
        .env("AGREP_DATA_READONLY", protected)
        .output()
        .expect("run index-lock-contract hold")
}

fn assert_protected_refusal(output: &Output, operation: &str) {
    assert!(
        !output.status.success(),
        "{operation} unexpectedly succeeded:\n{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("inside AGREP_DATA_READONLY"),
        "{operation} did not disclose the protected-root refusal:\n{stderr}"
    );
}

#[test]
fn semantic_q8_build_refuses_nested_protected_output_before_mutation() {
    let tree = TempRoot::new("semantic");
    let protected = tree.path().join("protected");
    fs::create_dir(&protected).unwrap();
    let output_dir = protected.join("derived").join("q8");

    let output = semantic_build(&protected, &output_dir);

    assert_protected_refusal(&output, "semantic-q8-build");
    assert!(!output_dir.exists());
    assert_eq!(fs::read_dir(&protected).unwrap().count(), 0);
}

#[test]
fn index_lock_hold_refuses_protected_path_before_mutation() {
    let tree = TempRoot::new("lock");
    let protected = tree.path().join("protected");
    fs::create_dir(&protected).unwrap();
    let lock = protected.join(".index.lock");

    let output = hold(&protected, &lock);

    assert_protected_refusal(&output, "index-lock-contract hold");
    assert!(!lock.exists());
    assert_eq!(fs::read_dir(&protected).unwrap().count(), 0);
}

#[test]
fn index_lock_hold_preserves_outside_root_behavior_and_segment_boundaries() {
    let tree = TempRoot::new("outside");
    let protected = tree.path().join("data");
    let outside = tree.path().join("database");
    fs::create_dir(&protected).unwrap();
    fs::create_dir(&outside).unwrap();
    let lock = outside.join(".index.lock");

    let output = hold(&protected, &lock);

    assert!(
        output.status.success(),
        "outside-root hold failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("\"ready\":true"),
        "outside-root hold did not reach acquisition"
    );
    assert!(!lock.exists(), "successful hold did not release its lock");
    assert_eq!(fs::read_dir(&protected).unwrap().count(), 0);
}

#[test]
fn semantic_q8_build_preserves_outside_root_dispatch() {
    let tree = TempRoot::new("semantic-outside");
    let protected = tree.path().join("data");
    let outside = tree.path().join("database");
    fs::create_dir(&protected).unwrap();
    fs::create_dir(&outside).unwrap();
    let output_dir = outside.join("q8");

    let output = semantic_build(&protected, &output_dir);

    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !stderr.contains("inside AGREP_DATA_READONLY"),
        "outside-root semantic build was falsely fenced:\n{stderr}"
    );
    assert!(
        stderr.contains("missing-meta.json")
            || stderr.contains("No such file")
            || stderr.contains("os error 2"),
        "outside-root semantic build did not reach its normal input validation:\n{stderr}"
    );
    assert_eq!(fs::read_dir(&protected).unwrap().count(), 0);
}

#[cfg(unix)]
#[test]
fn protected_root_guard_resolves_symlink_aliases_for_missing_targets() {
    use std::os::unix::fs::symlink;

    let tree = TempRoot::new("symlink");
    let protected = tree.path().join("protected");
    let alias = tree.path().join("alias");
    fs::create_dir(&protected).unwrap();
    symlink(&protected, &alias).unwrap();
    let output_dir = alias.join("derived").join("q8");

    let output = semantic_build(&protected, &output_dir);

    assert_protected_refusal(&output, "semantic-q8-build through symlink");
    assert!(!protected.join("derived").exists());
}

#[cfg(windows)]
#[test]
fn protected_root_guard_normalizes_windows_path_case() {
    let tree = TempRoot::new("windows-case");
    let protected = tree.path().join("MixedCaseData");
    fs::create_dir(&protected).unwrap();
    let protected_different_case = PathBuf::from(protected.to_string_lossy().to_uppercase());
    let output_dir = PathBuf::from(protected.to_string_lossy().to_lowercase()).join("derived-q8");

    let output = semantic_build(&protected_different_case, &output_dir);

    assert_protected_refusal(&output, "case-varied semantic-q8-build");
    assert!(!protected.join("derived-q8").exists());
}
