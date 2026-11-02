use std::path::PathBuf;
/// A validation run may pin its just-built executable while another worktree
/// builds into the same Cargo target. Missing executables always fail the tests.
pub fn binary() -> PathBuf {
    let path = std::env::var_os("AGREP_TEST_BINARY")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_agrep")));
    assert!(
        path.is_file(),
        "test executable is missing: {}",
        path.display()
    );
    path
}
