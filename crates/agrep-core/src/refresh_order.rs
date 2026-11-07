//! Native discovery keys for sources of the sessions being replayed.
use std::{
    collections::HashMap,
    ffi::OsString,
    fs,
    path::{Component, Path, PathBuf},
};

pub(crate) struct Order {
    roots: Vec<PathBuf>,
    directories: HashMap<PathBuf, Option<HashMap<OsString, usize>>>,
}
impl Order {
    pub(crate) fn new(roots: Vec<PathBuf>) -> Self {
        Self {
            roots,
            directories: HashMap::new(),
        }
    }
    fn position(&mut self, directory: &Path, name: &std::ffi::OsStr) -> Option<usize> {
        let names = self
            .directories
            .entry(directory.to_owned())
            .or_insert_with(|| {
                let metadata = fs::symlink_metadata(directory).ok()?;
                if !metadata.is_dir() || crate::ingest::registry::metadata_is_link(&metadata) {
                    return None;
                }
                fs::read_dir(directory)
                    .ok()?
                    .enumerate()
                    .map(|(index, entry)| entry.map(|entry| (entry.file_name(), index)))
                    .collect::<std::io::Result<HashMap<_, _>>>()
                    .ok()
            });
        names.as_ref()?.get(name).copied()
    }
    pub(crate) fn rank(&mut self, path: &Path) -> Option<Vec<usize>> {
        let metadata = fs::symlink_metadata(path).ok()?;
        if !metadata.is_file() || crate::ingest::registry::metadata_is_link(&metadata) {
            return None;
        }
        let (root, relative) = self
            .roots
            .iter()
            .enumerate()
            .find_map(|(i, root)| path.strip_prefix(root).ok().map(|p| (i, p)))?;
        let components: Vec<_> = relative
            .components()
            .map(|c| match c {
                Component::Normal(name) => Some(name.to_owned()),
                _ => None,
            })
            .collect::<Option<_>>()?;
        if components.len() < 2 {
            return None;
        }
        let mut key = vec![root];
        let mut directory = self.roots[root].clone();
        for (i, name) in components.iter().enumerate() {
            // Root slugs keep their native order. Within each slug, files
            // precede child directories, including at deeper levels.
            if i > 0 {
                key.push(usize::from(i + 1 < components.len()));
            }
            key.push(self.position(&directory, name)?);
            directory.push(name);
        }
        Some(key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn selective_keys_equal_native_discovery_order() {
        let temp = tempfile::tempdir().unwrap();
        let roots: Vec<_> = (0..4)
            .map(|n| temp.path().join(format!("root{n}")))
            .collect();
        for root in &roots {
            for name in [
                "z.jsonl",
                "a.jsonl",
                "one/z.jsonl",
                "one/a.jsonl",
                "one/child/z.jsonl",
                "one/child/deeper/a.jsonl",
                "two/b.jsonl",
                "two/child/a.jsonl",
                "ignore.txt",
            ] {
                let path = root.join(name);
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(path, "{}\n").unwrap();
            }
        }
        let expected: Vec<_> = roots
            .iter()
            .flat_map(|r| crate::ingest::pi::session_files("pi", r))
            .collect();
        let mut selected = Order::new(roots.clone());
        assert!(selected.rank(&expected[0]).is_some());
        assert!(selected
            .directories
            .keys()
            .all(|directory| expected[0].starts_with(directory)));
        assert!(selected.rank(&roots[0].join("z.jsonl")).is_none());
        let mut order = Order::new(roots);
        let ranks: Vec<_> = expected.iter().map(|p| order.rank(p).unwrap()).collect();
        for (i, left) in ranks.iter().enumerate() {
            for (j, right) in ranks.iter().enumerate() {
                assert_eq!(left.cmp(right), i.cmp(&j), "native positions {i} and {j}");
            }
        }
        assert!(order.rank(&temp.path().join("absent.jsonl")).is_none());
    }
}
