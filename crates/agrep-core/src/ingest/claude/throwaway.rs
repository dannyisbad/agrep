//! Throwaway workers: transcripts of Claude runs launched from a temp worker directory,
//! recognized structurally by their project folder name and accounted as named skips.

use std::ffi::OsStr;
use std::path::Path;

pub(super) fn is_throwaway_path(path: &Path) -> bool {
    path.parent().is_some_and(|parent| {
        parent
            .components()
            .any(|component| is_throwaway_name(component.as_os_str()))
    })
}

fn is_throwaway_name(name: &OsStr) -> bool {
    use std::sync::OnceLock;

    static TEMP_WORKER_PREFIXES: OnceLock<Vec<String>> = OnceLock::new();
    let prefixes = TEMP_WORKER_PREFIXES.get_or_init(|| {
        let temp = std::env::temp_dir();
        let mut roots = vec![temp.clone()];
        if let Ok(canonical) = temp.canonicalize() {
            if canonical != temp {
                roots.push(canonical);
            }
        }
        roots
            .iter()
            .map(|root| claude_worker_temp_prefix(root))
            .collect()
    });
    is_throwaway_name_for_prefixes(name, prefixes)
}

fn claude_worker_temp_prefix(root: &Path) -> String {
    // Claude's project-folder encoding replaces every non-ASCII-alphanumeric
    // character with '-'. Re-encode the actual OS temp root instead of trying
    // to decode a lossy slug or accepting any nested directory named "tmp".
    let mut slug: String = root
        .to_string_lossy()
        .chars()
        .map(|value| {
            if value.is_ascii_alphanumeric() {
                value.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    slug.push_str("-claude-worker-");
    slug
}

fn is_throwaway_name_for_prefixes(name: &OsStr, prefixes: &[String]) -> bool {
    // tempfile adds exactly eight characters. An underscore in that suffix is
    // encoded as '-' (for example `_zgq26av` becomes `-zgq26av`).
    let name = name.to_string_lossy().to_ascii_lowercase();
    prefixes.iter().any(|prefix| {
        name.strip_prefix(prefix).is_some_and(|token| {
            token.len() == 8
                && token
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        })
    })
}

/// Count a throwaway transcript's records as seen and skipped.
pub(super) fn tally_throwaway(
    tally: &crate::intake::Tally,
    data: &(impl crate::ingest::jsonl::Text + ?Sized),
) {
    // Keep byte-for-byte parity with audit's binary `line.strip()` census:
    // ASCII-whitespace-only lines are blank; non-ASCII bytes remain records.
    let records = data
        .lines()
        .filter(|line| line.bytes().any(|byte| !byte.is_ascii_whitespace()))
        .count() as u64;
    tally.seen_n(records);
    tally.skip_n(crate::intake::Skip::Throwaway, records);
}
