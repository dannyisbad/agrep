use agrep_core::{archive, data::DataRoot};
use anyhow::{bail, Result};
use std::path::{Component, Path, PathBuf};
pub fn run(needle: String, to: Option<String>, force: bool) -> Result<i32> {
    if needle.trim().is_empty() {
        bail!("needle must not be blank")
    }
    if to.as_ref().is_some_and(|s| s.trim().is_empty()) {
        bail!("--to must not be blank")
    }
    let to = to.map(|s| {
        let p = PathBuf::from(&s);
        let absolute = if s == "~" {
            agrep_core::ingest::home()
        } else if let Some(tail) = s.strip_prefix("~/") {
            agrep_core::ingest::home().join(tail)
        } else if p.is_absolute() {
            p
        } else {
            std::env::current_dir().unwrap_or_default().join(p)
        };
        resolved(&absolute)
    });
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let (_, errors) = archive::restore(&root, &needle, to.as_deref(), force)?;
    for e in &errors {
        agrep_core::outln!("{}", agrep_core::render::safe(e))?;
    }
    Ok(if errors.is_empty() { 0 } else { 1 })
}

/// `path` with its `.` and `..` resolved as a shell resolves them. They are the user's
/// own steps to the destination; the traversal check is for the archived paths below it.
fn resolved(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            part => out.push(part),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::resolved;
    use std::path::{Path, PathBuf};

    #[test]
    fn a_destination_resolves_its_own_dots() {
        let root = std::env::current_dir().unwrap();
        let root = root.ancestors().last().unwrap();
        for (given, expected) in [
            ("w/sub/../rto/x", "w/rto/x"),
            ("w/./sub/./x/.", "w/sub/x"),
            ("w/sub/../../../x", "x"),
            ("w/a/b/../../c", "w/c"),
        ] {
            assert_eq!(resolved(&root.join(given)), root.join(expected), "{given}");
        }
        assert_eq!(resolved(&root.join("..")), PathBuf::from(root));
        assert_eq!(resolved(Path::new(root)), PathBuf::from(root));
    }
}
