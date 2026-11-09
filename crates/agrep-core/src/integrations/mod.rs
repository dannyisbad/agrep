//! Consent-driven integrations. Enrollment describes only objects this install wrote.
mod settings;
use crate::ingest::registry::path_eq;
use crate::{data::DataRoot, safe_fs};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

const START: &str = "<!-- agrep:recall v1 -->";
const END: &str = "<!-- /agrep:recall -->";
const SKILL_HEADER: &str =
    "---\nname: agrep-recall\ndescription: Recover prior agent conversations when the user says again, like last time, refers to prior work, or an error or failure seems familiar.\n---\n";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Installed {
    pub path: PathBuf,
    pub kind: Kind,
    pub created: bool,
    #[serde(default)]
    pub directories: Vec<PathBuf>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Kind {
    Block {
        separator: String,
        skill: bool,
    },
    File {
        sha256: String,
    },
    Hook {
        event: String,
        entry: Value,
        #[serde(default)]
        added: Added,
    },
}
/// The outermost member setup added for a hook entry: remove takes it out again once it is
/// empty, and leaves alone any container that was the user's. Records without it come from
/// development builds, which always removed emptied containers up to `hooks`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Added {
    Entry,
    Event,
    #[default]
    Hooks,
}
#[derive(Debug, Clone)]
pub struct Target {
    pub name: String,
    pub path: PathBuf,
    pub detected: bool,
    pub skill: bool,
}

/// The registry data contains path *parts*, so joining follows the host OS convention.
/// opencode keeps its sessions under its data directory but reads global rules from its
/// config directory, so the data directory only proves the client is installed.
pub fn targets(home: &Path) -> Vec<Target> {
    let mut values = targets_in(home, &std::env::vars_os().collect());
    let data = crate::ingest::opencode::data_dirs_at(home);
    for target in &mut values {
        if target.name == "opencode" {
            target.detected = data[0].is_dir();
        }
    }
    values
}
/// Where development builds wrote opencode's block, which opencode never reads.
fn misplaced_paths(home: &Path) -> Vec<PathBuf> {
    crate::ingest::opencode::data_dirs_at(home)
        .into_iter()
        .map(|dir| dir.join("AGENTS.md"))
        .collect()
}
pub fn targets_in(
    home: &Path,
    env: &std::collections::BTreeMap<std::ffi::OsString, std::ffi::OsString>,
) -> Vec<Target> {
    let registry: Value =
        serde_json::from_str(include_str!("clients.json")).expect("embedded registry");
    let mut targets: Vec<Target> = registry["teach_targets"]
        .as_array()
        .expect("targets")
        .iter()
        .map(|v| {
            let path = |key: &str| {
                let base = if v[key]["base"] == "opencode_data" {
                    home.join(".local/share/opencode")
                } else {
                    home.to_path_buf()
                };
                v[key]["parts"]
                    .as_array()
                    .expect("path parts")
                    .iter()
                    .fold(base, |p, s| p.join(s.as_str().expect("part")))
            };
            Target {
                name: v["id"].as_str().unwrap().into(),
                path: path("target"),
                detected: path("proof").is_dir(),
                skill: v["kind"] == "skill",
            }
        })
        .collect();
    if let Some(raw) = env.get(std::ffi::OsStr::new("CODEX_HOME")).cloned() {
        let path = PathBuf::from(raw);
        if path.is_absolute() && path != home.join(".codex") {
            targets.push(Target {
                name: "codex".into(),
                path: path.join("AGENTS.md"),
                detected: path.is_dir(),
                skill: false,
            });
        }
    }
    if let Some(raw) = env.get(std::ffi::OsStr::new("XDG_CONFIG_HOME")).cloned() {
        let path = PathBuf::from(raw);
        if path.is_absolute() {
            for target in &mut targets {
                if let Ok(relative) = target.path.strip_prefix(home.join(".config")) {
                    target.path = path.join(relative);
                }
            }
        }
    }
    targets
}

fn spans(raw: &str) -> Result<Vec<std::ops::Range<usize>>> {
    let re = regex::Regex::new(r"(?m)^<!--\s*agrep:recall(?:[^\r\n]*?)-->[^\S\r\n]*(?:\r?\n)?")?;
    let end = regex::Regex::new(r"(?m)^<!--\s*/agrep:recall\s*-->[^\S\r\n]*(?:\r?\n)?")?;
    let mut spans = Vec::new();
    let mut offset = 0;
    while let Some(start) = re.find_at(raw, offset) {
        let stop = end
            .find_at(raw, start.end())
            .context("unclosed agrep instruction marker")?;
        if re
            .find_at(raw, start.end())
            .is_some_and(|m| m.start() < stop.start())
        {
            bail!("nested agrep instruction markers")
        }
        spans.push(start.start()..stop.end());
        offset = stop.end();
    }
    Ok(spans)
}

fn missing_directories(path: &Path) -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    let mut parent = path.parent();
    while let Some(p) = parent {
        if p.exists() {
            break;
        }
        dirs.push(p.to_path_buf());
        parent = p.parent();
    }
    dirs
}

pub fn install_block(target: &Target) -> Result<Installed> {
    let directories = missing_directories(&target.path);
    let prior = safe_fs::read_optional(&target.path)?;
    let raw = std::str::from_utf8(prior.as_deref().unwrap_or_default())
        .context("instruction file is not UTF-8")?;
    let ranges = spans(raw)?;
    if target.skill && prior.is_some() && ranges.is_empty() {
        bail!("existing skill is user-owned: {}", target.path.display())
    }
    let eol = if raw.contains("\r\n") { "\r\n" } else { "\n" };
    let nudge = if target.name == "codex" {
        include_str!("nudge_codex.md")
    } else {
        include_str!("nudge_default.md")
    };
    let block = format!("{START}\n{nudge}\n{END}\n").replace('\n', eol);
    let mut new = String::new();
    let mut offset = 0;
    for (i, r) in ranges.iter().enumerate() {
        new.push_str(&raw[offset..r.start]);
        if i == 0 {
            new.push_str(&block);
        }
        offset = r.end;
    }
    new.push_str(&raw[offset..]);
    let mut separator = String::new();
    if ranges.is_empty() {
        if target.skill {
            new.push_str(SKILL_HEADER);
        } else if !new.is_empty() {
            let n = if new.ends_with(&format!("{eol}{eol}")) {
                0
            } else if new.ends_with('\n') {
                1
            } else {
                2
            };
            separator = eol.repeat(n);
            new.push_str(&separator);
        }
        new.push_str(&block);
    }
    safe_fs::write_atomic(&target.path, new.as_bytes(), true)?;
    Ok(Installed {
        path: target.path.clone(),
        kind: Kind::Block {
            separator,
            skill: target.skill,
        },
        created: prior.is_none(),
        directories,
    })
}

fn hash(bytes: &[u8]) -> String {
    crate::store::hex_digest(Sha256::digest(bytes))
}
fn install_file(path: PathBuf, bytes: &[u8], previous: &[Installed]) -> Result<Installed> {
    let directories = missing_directories(&path);
    let old = safe_fs::read_optional(&path)?;
    if let Some(ref b) = old {
        let owned = previous.iter().any(|i| {
            path_eq(&i.path, &path)
                && matches!(&i.kind, Kind::File { sha256 } if *sha256 == hash(b))
        });
        if b != bytes && !owned {
            bail!("file has user edits: {}", path.display())
        }
    }
    safe_fs::write_atomic(&path, bytes, true)?;
    Ok(Installed {
        path,
        kind: Kind::File {
            sha256: hash(bytes),
        },
        created: old.is_none(),
        directories,
    })
}
fn install_hook(path: PathBuf, event: &str, entry: Value) -> Result<Installed> {
    let directories = missing_directories(&path);
    let old = safe_fs::read_optional(&path)?;
    let value: Value = match &old {
        Some(b) => serde_json::from_slice(b).context("invalid hook JSON")?,
        None => json!({}),
    };
    let object = value.as_object().context("hook JSON must be an object")?;
    if let Some(hooks) = object.get("hooks") {
        let hooks = hooks.as_object().context("hooks must be an object")?;
        if let Some(entries) = hooks.get(event) {
            entries.as_array().context("hook event must be an array")?;
        }
    }
    let raw = std::str::from_utf8(old.as_deref().unwrap_or_default())?;
    let (new, added) = settings::add(raw, event, &entry)?;
    if old.is_none() || new != raw {
        safe_fs::write_atomic(&path, new.as_bytes(), true)?;
    }
    Ok(Installed {
        path,
        kind: Kind::Hook {
            event: event.into(),
            entry,
            added,
        },
        created: old.is_none(),
        directories,
    })
}

fn codex_dirs(home: &Path) -> Vec<PathBuf> {
    let mut dirs = vec![home.join(".codex")];
    if let Some(raw) = std::env::var_os("CODEX_HOME") {
        let path = PathBuf::from(raw);
        if path.is_absolute() && !dirs.contains(&path) {
            dirs.push(path);
        }
    }
    dirs.into_iter().filter(|p| p.is_dir()).collect()
}
fn extension_dirs(home: &Path) -> Vec<PathBuf> {
    let mut omp = home.join(".omp/agent");
    if let Some(raw) = std::env::var_os("OMP_PROFILE").or_else(|| std::env::var_os("PI_PROFILE")) {
        let profile = PathBuf::from(raw);
        if profile.components().count() == 1
            && matches!(
                profile.components().next(),
                Some(std::path::Component::Normal(_))
            )
        {
            omp = home.join(".omp/profiles").join(profile).join("agent");
        }
    }
    [home.join(".pi/agent"), omp]
        .into_iter()
        .filter(|p| p.is_dir())
        .collect()
}
pub fn hook_paths(home: &Path) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if home.join(".claude").is_dir() {
        paths.extend([home.join(".claude/settings.json")]);
    }
    paths.extend(codex_dirs(home).iter().map(|p| p.join("hooks.json")));
    paths.extend(
        extension_dirs(home)
            .iter()
            .map(|p| p.join("extensions/agrep-postcompact.ts")),
    );
    paths
}

/// One object of an install: the same path and the same kind of entry.
fn same_object(a: &Installed, b: &Installed) -> bool {
    path_eq(&a.path, &b.path) && std::mem::discriminant(&a.kind) == std::mem::discriminant(&b.kind)
}
/// A reinstalled object keeps what its first install recorded: whether it created the
/// file, the directories it made, a block's separator and the containers a hook added.
fn inherit(item: &mut Installed, previous: &[Installed]) {
    let Some(old) = previous.iter().find(|i| same_object(i, item)) else {
        return;
    };
    item.created = old.created;
    item.directories.extend(old.directories.clone());
    item.directories.sort();
    item.directories.dedup();
    match (&mut item.kind, &old.kind) {
        (
            Kind::Block { separator, .. },
            Kind::Block {
                separator: old_separator,
                ..
            },
        ) => *separator = old_separator.clone(),
        (
            Kind::Hook { added, .. },
            Kind::Hook {
                added: old_added, ..
            },
        ) => {
            *added = (*added).max(*old_added);
        }
        _ => {}
    }
}
/// An error line that names the file to fix, once.
fn located(path: &Path, e: &anyhow::Error) -> String {
    let message = format!("{e:#}");
    let shown = path.display().to_string();
    if message.contains(&shown) {
        message
    } else {
        format!("{shown}: {message}")
    }
}
/// Install the selected files; persist each successful object before moving to the next.
/// A partial failure leaves sufficient enrollment for a later remove.
pub fn install(root: &DataRoot, teach: bool, hooks: bool) -> Result<Vec<String>> {
    let _lock = root.lock(true)?;
    let home = crate::ingest::home();
    let mut config = root.config()?;
    let mut errors = Vec::new();
    retire_misplaced(&home, &mut config, &mut errors);
    let previous = config.enrollment.clone();
    let mut record = |path: &Path, result: Result<Installed>| -> Result<()> {
        match result {
            Ok(mut item) => {
                inherit(&mut item, &previous);
                config.enrollment.retain(|i| !same_object(i, &item));
                config.enrollment.push(item);
                root.save_config(&config)?;
            }
            Err(e) => errors.push(located(path, &e)),
        }
        Ok(())
    };
    if teach {
        for target in targets(&home).into_iter().filter(|t| t.detected) {
            record(&target.path, install_block(&target))?;
        }
    }
    if hooks {
        install_hooks(&home, &previous, &mut record)?;
    }
    root.save_config(&config)?;
    Ok(errors)
}
/// Removes enrolled blocks that development builds wrote where opencode never reads them,
/// unless that path is also a current target.
fn retire_misplaced(home: &Path, config: &mut crate::data::Config, errors: &mut Vec<String>) {
    let current = targets(home);
    let misplaced: Vec<PathBuf> = misplaced_paths(home)
        .into_iter()
        .filter(|p| !current.iter().any(|t| path_eq(&t.path, p)))
        .collect();
    let mut directories = Vec::new();
    config.enrollment.retain(|item| {
        if !matches!(item.kind, Kind::Block { .. })
            || !misplaced.iter().any(|p| path_eq(p, &item.path))
        {
            return true;
        }
        match remove_one(item) {
            Ok(()) => {
                directories.extend(item.directories.clone());
                false
            }
            Err(e) => {
                errors.push(located(&item.path, &e));
                true
            }
        }
    });
    remove_directories(directories, errors);
}
/// The post-compaction hook of every detected client.
fn install_hooks(
    home: &Path,
    previous: &[Installed],
    record: &mut impl FnMut(&Path, Result<Installed>) -> Result<()>,
) -> Result<()> {
    if home.join(".claude").is_dir() {
        let path = home.join(".claude/settings.json");
        record(
            &path,
            install_hook(
                path.clone(),
                "PreCompact",
                json!({"matcher":"manual|auto","hooks":[{"type":"command","command":"agrep __hook claude"}]}),
            ),
        )?;
    }
    for dir in codex_dirs(home) {
        let path = dir.join("hooks.json");
        record(
            &path,
            install_hook(
                path.clone(),
                "SessionStart",
                json!({"matcher":"^compact$","hooks":[{"type":"command","command":"agrep __hook codex","timeout":2,"statusMessage":"Post-compact recovery available","additionalContextLimit":700}]}),
            ),
        )?;
    }
    for base in extension_dirs(home) {
        let path = base.join("extensions/agrep-postcompact.ts");
        record(
            &path,
            install_file(path.clone(), include_bytes!("pi_postcompact.ts"), previous),
        )?;
    }
    Ok(())
}

/// The file's text and its agrep blocks, or why they cannot be found.
fn blocks(raw: &[u8]) -> Result<(&str, Vec<std::ops::Range<usize>>)> {
    let raw = std::str::from_utf8(raw).context("instruction file is not UTF-8")?;
    Ok((raw, spans(raw)?))
}
pub fn remove_block(path: &Path, separator: &str, skill: bool, created: bool) -> Result<()> {
    let Some(raw) = safe_fs::read_optional(path)? else {
        return Ok(());
    };
    let (raw, ranges) = blocks(&raw)?;
    let mut new = String::new();
    let mut offset = 0;
    for (i, r) in ranges.iter().enumerate() {
        let prefix = &raw[offset..r.start];
        if i == 0 && !separator.is_empty() && prefix.ends_with(separator) {
            new.push_str(&prefix[..prefix.len() - separator.len()]);
        } else {
            new.push_str(prefix);
        }
        offset = r.end;
    }
    new.push_str(&raw[offset..]);
    if new == raw {
        return Ok(());
    }
    if created && (new.is_empty() || skill && new == SKILL_HEADER) {
        safe_fs::remove(path)
    } else {
        safe_fs::write_atomic(path, new.as_bytes(), true)
    }
}
fn remove_one(item: &Installed) -> Result<()> {
    match &item.kind {
        Kind::Block { separator, skill } => {
            remove_block(&item.path, separator, *skill, item.created)
        }
        Kind::File { sha256 } => {
            if let Some(bytes) = safe_fs::read_optional(&item.path)? {
                if hash(&bytes) != *sha256 {
                    bail!("file has user edits: {}", item.path.display())
                }
                safe_fs::remove(&item.path)?;
            }
            Ok(())
        }
        Kind::Hook {
            event,
            entry,
            added,
        } => {
            let Some(old) = safe_fs::read_optional(&item.path)? else {
                return Ok(());
            };
            serde_json::from_slice::<Value>(&old)?;
            let raw = std::str::from_utf8(&old)?;
            let new = settings::remove(raw, event, entry, *added)?;
            if new == raw {
                Ok(())
            } else if item.created && serde_json::from_str::<Value>(&new)? == json!({}) {
                safe_fs::remove(&item.path)
            } else {
                safe_fs::write_atomic(&item.path, new.as_bytes(), true)
            }
        }
    }
}
/// What `remove` could not do, and the unreadable files it left alone because it has no
/// record of writing them.
#[derive(Debug, Default)]
pub struct Removal {
    pub errors: Vec<String>,
    pub skipped: Vec<String>,
}
pub fn remove(root: &DataRoot) -> Result<Removal> {
    let _lock = root.lock(true)?;
    let home = crate::ingest::home();
    let mut config = root.config()?;
    let mut removal = Removal::default();
    let mut remaining = Vec::new();
    let mut directories = Vec::new();
    for item in &config.enrollment {
        match remove_one(item) {
            Ok(()) => directories.extend(item.directories.clone()),
            Err(e) => {
                removal.errors.push(located(&item.path, &e));
                remaining.push(item.clone());
            }
        }
    }
    let mut unenrolled: Vec<(PathBuf, bool)> = Vec::new();
    let known = targets(&home).into_iter().map(|t| (t.path, t.skill));
    for (path, skill) in known.chain(misplaced_paths(&home).into_iter().map(|p| (p, false))) {
        let seen = config.enrollment.iter().map(|i| &i.path);
        if !seen
            .chain(unenrolled.iter().map(|(p, _)| p))
            .any(|p| path_eq(p, &path))
        {
            unenrolled.push((path, skill));
        }
    }
    for (path, skill) in unenrolled {
        scrub_unenrolled(&path, skill, &mut removal);
    }
    remove_directories(directories, &mut removal.errors);
    config.enrollment = remaining;
    root.save_config(&config)?;
    Ok(removal)
}
/// A known target with no enrollment may still hold a block an earlier install wrote.
/// A file whose markers cannot be read is the user's own, so it is reported and kept.
fn scrub_unenrolled(path: &Path, skill: bool, removal: &mut Removal) {
    let result = match safe_fs::read_optional(path) {
        Ok(Some(raw)) => match blocks(&raw) {
            Ok(_) => remove_block(path, "", skill, false),
            Err(e) => return removal.skipped.push(located(path, &e)),
        },
        Ok(None) => Ok(()),
        Err(e) => Err(e),
    };
    if let Err(e) = result {
        removal.errors.push(located(path, &e));
    }
}
fn remove_directories(mut directories: Vec<PathBuf>, errors: &mut Vec<String>) {
    directories.sort_by_key(|p| std::cmp::Reverse(p.components().count()));
    directories.dedup();
    for dir in directories {
        if let Err(e) = safe_fs::plain_path(&dir) {
            errors.push(format!("{}: {e}", dir.display()));
            continue;
        }
        match std::fs::remove_dir(&dir) {
            Ok(()) => {}
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::DirectoryNotEmpty
                ) => {}
            Err(e) => errors.push(format!("{}: {e}", dir.display())),
        }
    }
}

/// Hidden command used by native client hooks; no interpreter or installed helper required.
pub fn hook(agent: &str, input: &str) -> Result<()> {
    match agent {
        "claude" => crate::out!("{}", include_str!("compact-contract.md"))?,
        "codex" => {
            let Ok(event) = serde_json::from_str::<Value>(input) else {
                return Ok(());
            };
            if input.len() > 65536
                || event.get("hook_event_name").and_then(Value::as_str) != Some("SessionStart")
            {
                return Ok(());
            }
            if event.get("source").and_then(Value::as_str) != Some("compact") {
                return Ok(());
            }
            let mut command = "agrep postcompact".to_owned();
            if let Some(id) = event.get("session_id").and_then(Value::as_str).filter(|s| {
                !s.is_empty()
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
            }) {
                command.push_str(&format!(" --session {id}"));
            }
            let guidance = include_str!("codex-postcompact.md")
                .replace("`agrep postcompact`", &format!("`{command}`"));
            crate::outln!(
                "{}",
                json!({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":guidance}})
            )?;
        }
        _ => bail!("unknown hook client"),
    }
    Ok(())
}

/// Check exactly what setup recorded, including owned hook entries and file digests.
pub fn enrolled_present(item: &Installed) -> Result<bool> {
    let Some(bytes) = safe_fs::read_optional(&item.path)? else {
        return Ok(false);
    };
    Ok(match &item.kind {
        Kind::Block { .. } => !spans(std::str::from_utf8(&bytes)?)?.is_empty(),
        Kind::File { sha256 } => hash(&bytes) == *sha256,
        Kind::Hook { event, entry, .. } => serde_json::from_slice::<Value>(&bytes)?["hooks"][event]
            .as_array()
            .is_some_and(|entries| entries.contains(entry)),
    })
}
/// Repairs only drifted enrollment; consent and archive policy are never changed.
pub fn repair(root: &DataRoot) -> Result<Vec<String>> {
    let _lock = root.lock(true)?;
    let home = crate::ingest::home();
    let mut config = root.config()?;
    let mut errors = Vec::new();
    let enrolled = config.enrollment.len();
    retire_misplaced(&home, &mut config, &mut errors);
    if config.enrollment.len() != enrolled {
        root.save_config(&config)?;
    }
    let targets = targets(&home);
    let mut widened = false;
    for i in 0..config.enrollment.len() {
        let item = &config.enrollment[i];
        if enrolled_present(item).unwrap_or(false) {
            continue;
        }
        let result = (|| -> Result<Option<Added>> {
            match &item.kind {
                Kind::Block { skill, .. } => {
                    let name = targets
                        .iter()
                        .find(|t| path_eq(&t.path, &item.path))
                        .map(|t| t.name.clone())
                        .unwrap_or_default();
                    install_block(&Target {
                        name,
                        path: item.path.clone(),
                        detected: true,
                        skill: *skill,
                    })?;
                }
                Kind::Hook { event, entry, .. } => {
                    if let Kind::Hook { added, .. } =
                        install_hook(item.path.clone(), event, entry.clone())?.kind
                    {
                        return Ok(Some(added));
                    }
                }
                Kind::File { .. } => {
                    if safe_fs::read_optional(&item.path)?.is_some() {
                        bail!("file has user edits: {}", item.path.display());
                    }
                    let bytes = match item.path.file_name().and_then(|v| v.to_str()) {
                        Some("agrep-postcompact.ts") => {
                            include_bytes!("pi_postcompact.ts").as_slice()
                        }
                        _ => bail!("unknown enrolled file: {}", item.path.display()),
                    };
                    safe_fs::write_atomic(&item.path, bytes, true)?;
                }
            }
            Ok(None)
        })();
        match result {
            // A container the repair had to add again is agrep's to remove.
            Ok(Some(now)) => {
                if let Kind::Hook { added, .. } = &mut config.enrollment[i].kind {
                    if now > *added {
                        *added = now;
                        widened = true;
                    }
                }
            }
            Ok(None) => {}
            Err(e) => errors.push(located(&config.enrollment[i].path, &e)),
        }
    }
    if widened {
        root.save_config(&config)?;
    }
    Ok(errors)
}
