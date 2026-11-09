//! What `claude --resume` needs from a resume: the directory that anchors a session, and the
//! session's transcript visible from the directory the agent starts in. Claude scopes resume
//! to `~/.claude/projects/<slug of the launch directory>/`.

use std::fs;
use std::io::{self, BufRead, BufReader};
use std::path::{Path, PathBuf};

use super::discovery::projects_root;
use crate::ingest::registry::metadata_is_link;

/// Claude Code's project-folder name for a directory: every character other than an ASCII
/// letter or digit becomes `-`.
fn slug(dir: &str) -> String {
    dir.chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect()
}

/// The session's plain transcripts directly under a project folder, in path order.
fn transcripts(root: &Path, session: &str) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(root) else {
        return Vec::new();
    };
    let name = format!("{session}.jsonl");
    let mut found: Vec<PathBuf> = entries
        .flatten()
        .filter(|e| e.file_name().as_encoded_bytes().first() != Some(&b'.'))
        .map(|e| e.path().join(&name))
        .filter(|p| fs::symlink_metadata(p).is_ok_and(|m| m.is_file() && !metadata_is_link(&m)))
        .collect();
    found.sort();
    found
}

/// The directory anchoring a session: of the distinct `cwd`s its transcript records, in order,
/// the first whose slug names the transcript's project folder (case-insensitively), else the
/// first recorded. The recorded `cwd` drifts as the agent changes directory, so neither the
/// first nor the busiest one is reliably where Claude filed the session
/// (py/hookless/native.py:244-276).
pub fn anchor_cwd(session: &str) -> Option<String> {
    if !crate::handles::plain(session) {
        return None;
    }
    transcripts(&projects_root(), session)
        .iter()
        .find_map(|path| anchor_in(path))
}

fn anchor_in(path: &Path) -> Option<String> {
    let folder = path.parent()?.file_name()?.to_string_lossy().to_lowercase();
    let mut reader = BufReader::new(fs::File::open(path).ok()?);
    let finder = memchr::memmem::Finder::new(b"\"cwd\"");
    let mut cwds: Vec<String> = Vec::new();
    let mut line = Vec::new();
    loop {
        line.clear();
        match reader.read_until(b'\n', &mut line) {
            Ok(0) => break,
            Ok(_) => {}
            Err(_) => return None,
        }
        if finder.find(&line).is_none() {
            continue;
        }
        let text = String::from_utf8_lossy(&line);
        let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
            continue;
        };
        match value.get("cwd").and_then(|c| c.as_str()) {
            Some(cwd) if !cwd.is_empty() && !cwds.iter().any(|seen| seen == cwd) => {
                cwds.push(cwd.to_owned())
            }
            _ => {}
        }
    }
    let anchored = cwds
        .iter()
        .position(|cwd| slug(cwd).to_lowercase() == folder);
    let index = anchored.or((!cwds.is_empty()).then_some(0))?;
    Some(cwds.swap_remove(index))
}

/// Makes the session's transcript visible from `cwd`'s project folder, as 0.4.9 did
/// (py/hookless/native.py:373-476): a symlink to the plain transcript, or a private copy
/// where links are refused. An existing plain file or agrep's own earlier link is left as it
/// is; anything else there is refused. Returns the note to print when something was made.
pub fn link_session(session: &str, cwd: &Path) -> io::Result<Option<String>> {
    if !crate::handles::plain(session) {
        return Ok(None);
    }
    let home = crate::ingest::home();
    let root = projects_root();
    let dst_dir = root.join(slug(&cwd.to_string_lossy()));
    let dst = dst_dir.join(format!("{session}.jsonl"));
    match fs::symlink_metadata(&dst) {
        Ok(meta) if meta.file_type().is_symlink() && own_link(&root, session, &dst) => {
            return Ok(None)
        }
        Ok(meta) if metadata_is_link(&meta) || !meta.is_file() => {
            return Err(unsafe_path("destination", &dst))
        }
        Ok(_) => return Ok(None),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
            ) => {}
        Err(error) => return Err(error),
    }
    let Some(src) = transcripts(&root, session).into_iter().next() else {
        return Ok(None);
    };
    ensure_parent(&home, &dst_dir)?;
    let how = match symlink(&src, &dst) {
        Ok(()) => "linked",
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return Err(unsafe_path("destination appeared", &dst))
        }
        Err(_) => {
            copy_private(&src, &dst)?;
            "copied"
        }
    };
    Ok(Some(format!(
        "↪ {how} transcript into {} so claude --resume finds it",
        crate::render::safe(&dst_dir.to_string_lossy())
    )))
}

fn unsafe_path(what: &str, path: &Path) -> io::Error {
    io::Error::other(format!("unsafe Claude resume {what}: {}", path.display()))
}

/// Exactly the link an earlier resume made: absolute, aimed at this session's plain transcript
/// in another project folder of the same store.
fn own_link(root: &Path, session: &str, dst: &Path) -> bool {
    let Ok(target) = fs::read_link(dst) else {
        return false;
    };
    let folder = target.parent();
    target.is_absolute()
        && target.file_name() == Some(std::ffi::OsStr::new(&format!("{session}.jsonl")))
        && folder.and_then(Path::parent).map(normal) == Some(normal(root))
        && folder.map(normal) != dst.parent().map(normal)
        && fs::symlink_metadata(&target).is_ok_and(|m| m.is_file() && !metadata_is_link(&m))
}

/// Lexical normalization, as Python's `os.path.normpath`.
fn normal(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir if out.file_name().is_some() => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    out
}

/// Creates each missing directory from home down to `dir`, refusing any that is a link or
/// not a directory.
fn ensure_parent(home: &Path, dir: &Path) -> io::Result<()> {
    let relative = dir
        .strip_prefix(home)
        .map_err(|_| unsafe_path("destination parent", dir))?;
    let mut current = home.to_path_buf();
    for part in std::iter::once(None).chain(relative.components().map(Some)) {
        if let Some(part) = part {
            current.push(part);
        }
        let meta = match fs::symlink_metadata(&current) {
            Ok(meta) => meta,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                match fs::create_dir(&current) {
                    Err(error) if error.kind() != io::ErrorKind::AlreadyExists => {
                        return Err(error)
                    }
                    _ => {}
                }
                fs::symlink_metadata(&current)?
            }
            Err(error) => return Err(error),
        };
        if metadata_is_link(&meta) || !meta.is_dir() {
            return Err(unsafe_path("destination parent", &current));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn symlink(src: &Path, dst: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(src, dst)
}

#[cfg(windows)]
fn symlink(src: &Path, dst: &Path) -> io::Result<()> {
    std::os::windows::fs::symlink_file(src, dst)
}

/// A new owner-only copy, never following a link at either end.
fn copy_private(src: &Path, dst: &Path) -> io::Result<()> {
    let mut read = fs::OpenOptions::new();
    read.read(true);
    let mut write = fs::OpenOptions::new();
    write.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        read.custom_flags(libc::O_NOFOLLOW);
        write.custom_flags(libc::O_NOFOLLOW).mode(0o600);
    }
    io::copy(&mut read.open(src)?, &mut write.open(dst)?)?;
    Ok(())
}
