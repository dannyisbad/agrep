//! Consent-driven integrations. Enrollment describes only objects this install wrote.
mod settings;
use crate::ingest::registry::path_eq;
use crate::{data::DataRoot, safe_fs};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use settings::render_json;
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
    Block { separator: String, skill: bool },
    File { sha256: String },
    Hook { event: String, entry: Value },
}
#[derive(Debug, Clone)]
pub struct Target {
    pub name: String,
    pub path: PathBuf,
    pub detected: bool,
    pub skill: bool,
}

/// The registry data contains path *parts*, so joining follows the host OS convention.
pub fn targets(home: &Path) -> Vec<Target> {
    let mut values = targets_in(home, &std::env::vars_os().collect());
    let data = crate::ingest::registry::ADAPTERS
        .iter()
        .find(|a| a.name() == "opencode")
        .expect("opencode adapter")
        .store_roots()
        .into_iter()
        .next()
        .expect("opencode data root");
    for target in &mut values {
        if target.name == "opencode" {
            target.path = data.join("AGENTS.md");
            target.detected = data.is_dir();
        }
    }
    values
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
    let mut value: Value = match &old {
        Some(b) => serde_json::from_slice(b).context("invalid hook JSON")?,
        None => json!({}),
    };
    let object = value
        .as_object_mut()
        .context("hook JSON must be an object")?;
    let hooks = object
        .entry("hooks")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("hooks must be an object")?;
    let entries = hooks
        .entry(event)
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .context("hook event must be an array")?;
    if !entries.contains(&entry) {
        entries.push(entry.clone());
    }
    safe_fs::write_atomic(
        &path,
        &render_json(&value, old.as_deref().unwrap_or_default())?,
        true,
    )?;
    Ok(Installed {
        path,
        kind: Kind::Hook {
            event: event.into(),
            entry,
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
/// file, the directories it made, and a block's separator.
fn inherit(item: &mut Installed, previous: &[Installed]) {
    let Some(old) = previous.iter().find(|i| same_object(i, item)) else {
        return;
    };
    item.created = old.created;
    item.directories.extend(old.directories.clone());
    item.directories.sort();
    item.directories.dedup();
    if let (
        Kind::Block { separator, .. },
        Kind::Block {
            separator: old_separator,
            ..
        },
    ) = (&mut item.kind, &old.kind)
    {
        *separator = old_separator.clone();
    }
}
/// Install the selected files; persist each successful object before moving to the next.
/// A partial failure leaves sufficient enrollment for a later remove.
pub fn install(root: &DataRoot, teach: bool, hooks: bool) -> Result<Vec<String>> {
    let _lock = root.lock(true)?;
    let home = crate::ingest::home();
    let mut config = root.config()?;
    let previous = config.enrollment.clone();
    let mut errors = Vec::new();
    let mut record = |result: Result<Installed>| -> Result<()> {
        match result {
            Ok(mut item) => {
                inherit(&mut item, &previous);
                config.enrollment.retain(|i| !same_object(i, &item));
                config.enrollment.push(item);
                root.save_config(&config)?;
            }
            Err(e) => errors.push(format!("{e:#}")),
        }
        Ok(())
    };
    if teach {
        for target in targets(&home).into_iter().filter(|t| t.detected) {
            record(install_block(&target))?;
        }
    }
    if hooks {
        install_hooks(&home, &previous, &mut record)?;
    }
    root.save_config(&config)?;
    Ok(errors)
}
/// The post-compaction hook of every detected client.
fn install_hooks(
    home: &Path,
    previous: &[Installed],
    record: &mut impl FnMut(Result<Installed>) -> Result<()>,
) -> Result<()> {
    if home.join(".claude").is_dir() {
        record(install_hook(
            home.join(".claude/settings.json"),
            "PreCompact",
            json!({"matcher":"manual|auto","hooks":[{"type":"command","command":"agrep __hook claude"}]}),
        ))?;
    }
    for dir in codex_dirs(home) {
        record(install_hook(
            dir.join("hooks.json"),
            "SessionStart",
            json!({"matcher":"^compact$","hooks":[{"type":"command","command":"agrep __hook codex","timeout":2,"statusMessage":"Post-compact recovery available","additionalContextLimit":700}]}),
        ))?;
    }
    for base in extension_dirs(home) {
        record(install_file(
            base.join("extensions/agrep-postcompact.ts"),
            include_bytes!("pi_postcompact.ts"),
            previous,
        ))?;
    }
    Ok(())
}

pub fn remove_block(path: &Path, separator: &str, skill: bool, created: bool) -> Result<()> {
    let Some(raw) = safe_fs::read_optional(path)? else {
        return Ok(());
    };
    let raw = std::str::from_utf8(&raw).context("instruction file is not UTF-8")?;
    let mut new = String::new();
    let mut offset = 0;
    let ranges = spans(raw)?;
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
        Kind::Hook { event, entry } => {
            let Some(old) = safe_fs::read_optional(&item.path)? else {
                return Ok(());
            };
            let mut value: Value = serde_json::from_slice(&old)?;
            if let Some(hooks) = value.get_mut("hooks").and_then(Value::as_object_mut) {
                if let Some(entries) = hooks.get_mut(event).and_then(Value::as_array_mut) {
                    entries.retain(|e| e != entry);
                    if entries.is_empty() {
                        hooks.shift_remove(event);
                    }
                }
                if hooks.is_empty() {
                    value.as_object_mut().unwrap().shift_remove("hooks");
                }
            }
            if item.created && value == json!({}) {
                safe_fs::remove(&item.path)
            } else {
                safe_fs::write_atomic(&item.path, &render_json(&value, &old)?, true)
            }
        }
    }
}
pub fn remove(root: &DataRoot) -> Result<Vec<String>> {
    let _lock = root.lock(true)?;
    let mut config = root.config()?;
    let mut errors = Vec::new();
    let mut remaining = Vec::new();
    let mut directories = Vec::new();
    let known = targets(&crate::ingest::home());
    for item in &config.enrollment {
        match remove_one(item) {
            Ok(()) => directories.extend(item.directories.clone()),
            Err(e) => {
                errors.push(format!("{}: {e:#}", item.path.display()));
                remaining.push(item.clone());
            }
        }
    }
    for target in known {
        if !config
            .enrollment
            .iter()
            .any(|i| path_eq(&i.path, &target.path))
        {
            if let Err(e) = remove_block(&target.path, "", target.skill, false) {
                errors.push(format!("{}: {e:#}", target.path.display()));
            }
        }
    }
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
    config.enrollment = remaining;
    root.save_config(&config)?;
    Ok(errors)
}

/// Hidden command used by native client hooks; no interpreter or installed helper required.
pub fn hook(agent: &str, input: &str) -> Result<()> {
    match agent {
        "claude" => print!("{}", include_str!("compact-contract.md")),
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
            println!(
                "{}",
                json!({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":guidance}})
            );
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
        Kind::Hook { event, entry } => serde_json::from_slice::<Value>(&bytes)?["hooks"][event]
            .as_array()
            .is_some_and(|entries| entries.contains(entry)),
    })
}
/// Repairs only drifted enrollment; consent and archive policy are never changed.
pub fn repair(root: &DataRoot) -> Result<Vec<String>> {
    let _lock = root.lock(true)?;
    let config = root.config()?;
    let targets = targets(&crate::ingest::home());
    let mut errors = Vec::new();
    for item in &config.enrollment {
        if enrolled_present(item).unwrap_or(false) {
            continue;
        }
        let result = (|| -> Result<()> {
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
                Kind::Hook { event, entry } => {
                    install_hook(item.path.clone(), event, entry.clone())?;
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
            Ok(())
        })();
        if let Err(e) = result {
            errors.push(format!("{}: {e:#}", item.path.display()));
        }
    }
    Ok(errors)
}

#[cfg(test)]
mod tests {
    use super::*;
    // Each expectation is what 0.4.9's `_render_settings` wrote for the same file and value.
    #[test]
    fn hooks_take_the_files_separators_and_escapes() {
        let hooks = json!({"Stop": [{"command": "café"}]});
        for (old, want) in [
            (
                r#"{"model": "opus"}"#,
                r#"{"model": "opus", "hooks": {"Stop": [{"command": "café"}]}}"#,
            ),
            (
                r#"{"model":"opus"}"#,
                r#"{"model":"opus","hooks":{"Stop":[{"command":"café"}]}}"#,
            ),
            (
                "{\n  \"a\": \"\\u00e9\"\n}\n",
                "{\n  \"a\": \"\\u00e9\",\n  \"hooks\": {\n    \"Stop\": [\n      {\n        \"command\": \"caf\\u00e9\"\n      }\n    ]\n  }\n}\n",
            ),
        ] {
            let mut value: Value = serde_json::from_str(old).unwrap();
            value["hooks"] = hooks.clone();
            let rendered = render_json(&value, old.as_bytes()).unwrap();
            assert_eq!(String::from_utf8(rendered).unwrap(), want);
        }
    }
}
