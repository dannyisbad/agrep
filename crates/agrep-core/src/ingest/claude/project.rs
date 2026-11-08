//! Project and side-session identity: the project a session worked in, voted from its cwds and
//! tool paths, and the context a transcript's location implies.

use std::collections::HashMap;
use std::path::Path;

use crate::ingest::project_name;

/// Pull file paths out of a session's tool_use blocks (Read/Write/Edit/... carry file_path,
/// notebook_path, or path). These reveal where the work actually happened.
pub(super) fn collect_tool_paths(content: &serde_json::Value, out: &mut Vec<String>) {
    let serde_json::Value::Array(blocks) = content else {
        return;
    };
    for b in blocks {
        if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
            continue;
        }
        let Some(inp) = b.get("input") else {
            continue;
        };
        for key in ["file_path", "notebook_path", "path"] {
            if let Some(v) = inp.get(key).and_then(|v| v.as_str()) {
                if v.contains('/') || v.contains('\\') {
                    out.push(v.to_string());
                }
            }
        }
    }
}

/// Reduce a directory to its project ROOT bucket: strip the home prefix and any
/// container segments (`Users/<name>/Desktop/...`, `projects/`, `repos/`), keep the
/// first real segment. `~/Desktop/projects/myproj/src` -> Some("myproj"); a bare home
/// dir or bare container -> None (no signal).
fn project_root(dir: &str) -> Option<String> {
    let d = dir.replace('\\', "/");
    let segs = segments_below_home(&d);
    let user = crate::ingest::home_leaf();
    let mut scan = RootScan::default();
    segs.into_iter()
        .find(|s| scan.is_root(s, user))
        .map(|s| s.to_string())
}

/// The non-empty segments of a slash-normalized path, minus a UNC host/share and the home prefix.
fn segments_below_home(d: &str) -> Vec<&str> {
    let mut segs: Vec<&str> = d.split('/').filter(|s| !s.is_empty()).collect();
    // UNC/WSL paths (//wsl.localhost/Ubuntu/..., //server/share/...) lead with host
    // and share segments that would otherwise win the bucket
    if d.starts_with("//") {
        segs.drain(..2.min(segs.len()));
    }
    let home = crate::ingest::home().to_string_lossy().replace('\\', "/");
    let home_segs: Vec<&str> = home.split('/').filter(|s| !s.is_empty()).collect();
    // strip home per component: a byte-prefix compare on a lowercased copy can slice
    // mid-char, and would false-match a shorter home prefix inside a longer username
    if segs.len() >= home_segs.len()
        && segs
            .iter()
            .zip(&home_segs)
            .all(|(a, b)| a.eq_ignore_ascii_case(b))
    {
        segs.drain(..home_segs.len());
    }
    segs
}

/// Left-to-right scan state over a path's segments, skipping containers and what they imply.
#[derive(Default)]
struct RootScan {
    // the segment right after a Users/home container is a username on ANY machine
    // (a migrated corpus's cwds carry another box's user) - home_leaf() only knows
    // this one's, so any post-Users/home segment is skipped unconditionally.
    after_user_container: bool,
    // macOS TMPDIR: /private/var/folders/<2 chars>/<random>/T/<name>
    after_folders: u8,
}

impl RootScan {
    fn is_root(&mut self, segment: &str, user: &str) -> bool {
        let sl = segment.to_ascii_lowercase();
        if sl.ends_with(':') || std::mem::take(&mut self.after_user_container) {
            return false;
        }
        if self.after_folders > 0 {
            self.after_folders -= 1;
            return false;
        }
        // Containers win over the home-leaf skip: a HOME ending in `home` or `users`
        // must not hide the container and promote the username to the project.
        if matches!(sl.as_str(), "users" | "home") {
            self.after_user_container = true;
            return false;
        }
        if sl == user {
            return false;
        }
        if sl == "folders" {
            self.after_folders = 3;
            return false;
        }
        !is_container(&sl)
    }
}

fn is_container(segment: &str) -> bool {
    matches!(
        segment,
        "desktop"
            | "documents"
            | "downloads"
            | "onedrive"
            | "tmp"
            | "temp"
            | "private"
            | "var"
            | "appdata"
            | "local"
            | "locallow"
            | "roaming"
            | "src"
            | "projects"
            | "project"
            | "code"
            | "repos"
            | "repositories"
            | "git"
            | "github"
            | "work"
            | "dev"
            | "workspace"
            | "workspaces"
    )
}

/// The project a session actually worked in: a histogram over EVERY line's cwd (Claude
/// updates it as the session cd's around) plus the parent dir of every file its tools
/// touched, reduced to project roots. The most-worked-in root wins, so sessions launched
/// from a home dir that evolve into a real project land on where the work went, not
/// where the terminal happened to open.
pub(super) fn primary_project(
    cwd_counts: &HashMap<String, usize>,
    paths: &[String],
    first_cwd: &str,
    fallback: &str,
    votes: &mut HashMap<String, usize>,
) -> String {
    let mut counts = std::mem::take(votes);
    for (c, n) in cwd_counts {
        if let Some(k) = project_root(c) {
            *counts.entry(k).or_insert(0) += n;
        }
    }
    for p in paths {
        let pn = p.replace('\\', "/");
        if let Some(i) = pn.rfind('/') {
            if let Some(k) = project_root(&pn[..i]) {
                *counts.entry(k).or_insert(0) += 1;
            }
        }
    }
    *votes = counts.clone();
    // ties break on name so re-ingest is deterministic
    if let Some((k, _)) = counts
        .into_iter()
        .max_by(|a, b| a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0)))
    {
        return k;
    }
    if first_cwd.is_empty() {
        fallback.to_string()
    } else {
        project_name(first_cwd)
    }
}

pub(super) fn directory_project(path: &Path) -> String {
    let parts: Vec<&str> = path.iter().filter_map(|part| part.to_str()).collect();
    if let Some(index) = parts.iter().rposition(|part| *part == "projects") {
        if let Some(slug) = parts.get(index + 1) {
            return (*slug).to_string();
        }
    }
    if let Some(index) = parts.iter().rposition(|part| *part == "subagents") {
        if let Some(slug) = index.checked_sub(2).and_then(|value| parts.get(value)) {
            return (*slug).to_string();
        }
    }
    path.parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .unwrap_or("claude")
        .to_string()
}

/// Side-session context, derived from the path so the cached per-file parse
/// signature stays untouched: `<project>/<parent-session>/subagents/**/agent-*.jsonl`
/// -> Some(parent session id). Top-level session files -> None.
pub(super) fn side_context(path: &Path) -> Option<String> {
    let mut comps: Vec<&str> = path.iter().filter_map(|c| c.to_str()).collect();
    comps.pop(); // the file itself
    let sub = comps.iter().rposition(|c| *c == "subagents")?;
    comps.get(sub.checked_sub(1)?).map(|s| s.to_string())
}
