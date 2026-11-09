use std::path::{Path, PathBuf};
/// `path` spelled as agrep prints it. On Windows `join("a/b")` keeps the `/`, while the
/// paths agrep finds and prints use `\` throughout.
pub fn native(path: &Path) -> PathBuf {
    path.components().collect()
}
