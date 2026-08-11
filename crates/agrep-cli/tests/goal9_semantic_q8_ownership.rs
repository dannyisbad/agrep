use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

const BIN: &str = env!("CARGO_BIN_EXE_agrep-rs");
const BUILD_A: &str = "aaaaaaaaaaaaaaaaaaaa";
const BUILD_B: &str = "bbbbbbbbbbbbbbbbbbbb";
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
            "agrep-goal9-q8-owner-{tag}-{}-{nanos}-{serial}",
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

fn write_owner(data: &Path, build_id: &str) {
    fs::write(
        data.join(".derived-owner.json"),
        format!(r#"{{"version":1,"build_id":"{build_id}"}}"#),
    )
    .unwrap();
}

fn write_inputs(root: &Path) -> (PathBuf, PathBuf, PathBuf) {
    let embeddings = root.join("input.f32");
    let meta = root.join("input.meta");
    let groups = root.join("groups.txt");
    let mut matrix = Vec::new();
    matrix.extend_from_slice(&1.0_f32.to_le_bytes());
    matrix.extend_from_slice(&0.0_f32.to_le_bytes());
    fs::write(&embeddings, matrix).unwrap();
    fs::write(
        &meta,
        concat!(
            r#"{"dim":2,"commit":{"version":1,"generation":"#,
            r#""00112233445566778899aabbccddeeff","rows":1,"matrix":{"size":8}}}"#,
        ),
    )
    .unwrap();
    fs::write(&groups, b"fixture\n").unwrap();
    (embeddings, meta, groups)
}

fn run_build(
    data: &Path,
    build_id: &str,
    inputs: &(PathBuf, PathBuf, PathBuf),
    output_dir: &Path,
) -> Output {
    Command::new(BIN)
        .args(["semantic-q8-build", "--embeddings"])
        .arg(&inputs.0)
        .arg("--meta")
        .arg(&inputs.1)
        .arg("--groups")
        .arg(&inputs.2)
        .arg("--output-dir")
        .arg(output_dir)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_RUNTIME_BUILD_ID", build_id)
        .env_remove("AGREP_DATA_READONLY")
        .env_remove("AGREP_PYTHON_RUNTIME_BUILD_ID")
        .output()
        .expect("run semantic-q8-build")
}

fn census(root: &Path) -> BTreeMap<String, Vec<u8>> {
    fn visit(root: &Path, current: &Path, out: &mut BTreeMap<String, Vec<u8>>) {
        let mut entries: Vec<_> = fs::read_dir(current)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect();
        entries.sort();
        for path in entries {
            let relative = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            if path.is_dir() {
                out.insert(format!("{relative}/"), Vec::new());
                visit(root, &path, out);
            } else {
                out.insert(relative, fs::read(path).unwrap());
            }
        }
    }

    let mut observed = BTreeMap::new();
    visit(root, root, &mut observed);
    observed
}

#[test]
fn semantic_q8_refuses_foreign_and_adoption_outputs_inside_data_dir() {
    let tree = TempRoot::new("refused");
    let inputs = write_inputs(tree.path());

    let foreign = tree.path().join("foreign-data");
    fs::create_dir(&foreign).unwrap();
    write_owner(&foreign, BUILD_B);
    let before = census(&foreign);
    let output_dir = foreign.join("semantic-q8").join("generation");
    let output = run_build(&foreign, BUILD_A, &inputs, &output_dir);
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains(&format!("owned-by {BUILD_B}")), "{stderr}");
    assert!(!output_dir.exists());
    assert_eq!(census(&foreign), before);

    let adoption = tree.path().join("adoption-data");
    fs::create_dir(&adoption).unwrap();
    let before = census(&adoption);
    let output_dir = adoption.join("semantic-q8");
    let output = run_build(&adoption, BUILD_A, &inputs, &output_dir);
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("ownership is not established"), "{stderr}");
    assert!(!output_dir.exists());
    assert_eq!(census(&adoption), before);
}

#[test]
fn semantic_q8_allows_current_inside_and_foreign_outside_data_dir() {
    let tree = TempRoot::new("allowed");
    let inputs = write_inputs(tree.path());

    let current = tree.path().join("current-data");
    fs::create_dir(&current).unwrap();
    write_owner(&current, BUILD_A);
    let current_output = current.join("semantic-q8");
    let output = run_build(&current, BUILD_A, &inputs, &current_output);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(fs::read_dir(&current_output).unwrap().count() >= 2);

    let foreign = tree.path().join("foreign-data");
    fs::create_dir(&foreign).unwrap();
    write_owner(&foreign, BUILD_B);
    let outside_output = tree.path().join("outside-q8");
    let output = run_build(&foreign, BUILD_A, &inputs, &outside_output);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(fs::read_dir(&outside_output).unwrap().count() >= 2);
    assert_eq!(
        census(&foreign),
        BTreeMap::from([(
            ".derived-owner.json".to_string(),
            fs::read(foreign.join(".derived-owner.json")).unwrap(),
        )]),
    );
}
