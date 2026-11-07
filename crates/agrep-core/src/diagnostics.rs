//! Read-only facts for diagnostic commands. No query parses source transcripts.
use crate::{data::DataRoot, store};
use anyhow::Result;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{fs, path::Path};
/// Bytes and files under `path`, without following links. Background workers and SQLite
/// create and delete files in the data root while this walks it, so an entry gone by the
/// time it is examined counts as absent.
pub fn disk_usage(path: &Path) -> Result<(u64, u64)> {
    let gone = |e: &std::io::Error| e.kind() == std::io::ErrorKind::NotFound;
    let m = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if gone(&e) => return Ok((0, 0)),
        Err(e) => return Err(e.into()),
    };
    if m.file_type().is_symlink() {
        return Ok((0, 0));
    }
    if m.is_file() {
        return Ok((m.len(), 1));
    }
    let entries = match fs::read_dir(path) {
        Ok(entries) => entries,
        Err(e) if gone(&e) => return Ok((0, 0)),
        Err(e) => return Err(e.into()),
    };
    let (mut bytes, mut files) = (0, 0);
    for e in entries {
        let (b, f) = disk_usage(&e?.path())?;
        (bytes, files) = (bytes + b, files + f);
    }
    Ok((bytes, files))
}
pub fn state(c: &Connection, key: &str) -> Result<Option<String>> {
    Ok(
        c.query_row("SELECT value FROM state WHERE key=?1", [key], |r| r.get(0))
            .optional()?,
    )
}
pub fn source_rows(c: &Connection) -> Result<Vec<Value>> {
    let mut q = c.prepare(
        "SELECT path,agent,size,mtime_ns,last_error,change_token FROM sources ORDER BY agent,path",
    )?;
    let result=q.query_map([],|r| {
        let path:String=r.get(0)?;let agent:String=r.get(1)?;
        Ok(json!({"path":source_path(&path,&agent),"derived":path.starts_with('@')||path.starts_with("token:"),"agent":agent,"bytes":r.get::<_,i64>(2)?,"mtime_ns":r.get::<_,i64>(3)?,"last_error":r.get::<_,Option<String>>(4)?,"change_token":r.get::<_,crate::store::SourceToken>(5)?.diagnostic()}))
    })?.collect::<rusqlite::Result<_>>()?;
    Ok(result)
}
pub fn status(root: &DataRoot) -> Result<Value> {
    let marker = fs::read(root.0.join("FORMAT"));
    let format = match marker {
        Ok(ref b) if b == crate::data::FORMAT => "valid",
        Ok(_) => "unsupported",
        Err(ref e) if e.kind() == std::io::ErrorKind::NotFound => "missing",
        Err(_) => "unreadable",
    };
    let usage = if root.0.exists() {
        Some(disk_usage(&root.0)?)
    } else {
        None
    };
    let mut out = json!({"version":env!("CARGO_PKG_VERSION"),"data_root":root.0,"format":format,"disk_bytes":usage.map(|u| u.0),"disk_files":usage.map(|u| u.1),"index":{"state":"missing"},"sources":[],"agents":[],"semantic":{"model_present":false,"indexed_rows":0,"total_rows":0,"backlog":0},"integrations":[],"archive":{"present":root.0.join("archive").is_dir()}});
    if format != "valid" {
        return Ok(out);
    }
    let mut exposed = Vec::new();
    for path in std::iter::once(root.path().to_path_buf())
        .chain(["archive", "model", "log"].map(|name| root.path().join(name)))
        .chain(std::iter::once(crate::semantic_model::directory(root)))
    {
        if path.is_dir() && crate::data::exposed(&path)? {
            exposed.push(path);
        }
    }
    out["permissions"] = json!({"private":exposed.is_empty(),"exposed":exposed});
    let config = root.config()?;
    out["integrations"] = json!(integrations(&config)?);
    out["archive"]["enabled"] = json!(config.archive.enabled);
    if !root.db().exists() {
        if root.path().join("index.building.db").exists() {
            out["index"]["state"] = json!(if root.writer_active()? {
                "first build in progress"
            } else {
                "first build interrupted"
            });
        }
        return Ok(out);
    }
    let c = match store::reader(root) {
        Ok(c) => c,
        Err(e) => {
            out["index"] = json!({"state":"unreadable","error":e.to_string()});
            return Ok(out);
        }
    };
    let counts = match store::counts(&c) {
        Ok(v) => v,
        Err(e) => {
            out["index"] = json!({"state":"unreadable","error":e.to_string()});
            return Ok(out);
        }
    };
    let refreshed: Option<i64> = state(&c, "refreshed")?.and_then(|ms| ms.parse().ok());
    out["index"] = json!({"state":"ready","counts":counts,"last_refresh":refreshed});
    out["last_indexed_age_s"] =
        json!(refreshed.map(|ms| (chrono::Utc::now().timestamp_millis() - ms).max(0) / 1000));
    if let Some(counts) = counts.as_object() {
        for (key, value) in counts {
            out[key] = value.clone();
        }
    }

    let sources = source_rows(&c)?;
    let mut agents = std::collections::BTreeMap::<String, (usize, usize)>::new();
    for source in &sources {
        let entry = agents
            .entry(source["agent"].as_str().unwrap_or("").into())
            .or_default();
        if source["derived"] != true {
            entry.0 += 1;
        }
        if !source["last_error"].is_null() {
            entry.1 += 1;
        }
    }
    out["agents"] = json!(agents
        .into_iter()
        .map(|(a, (n, e))| json!({"agent":a,"sources":n,"parse_errors":e}))
        .collect::<Vec<_>>());
    out["sources"] = json!(sources);
    let coverage = crate::semantic::coverage(&c)?;
    let installed = crate::semantic::installed(root);
    let progress = crate::semantic::build_progress(&c, &coverage)?;
    out["semantic"] = json!({"model_present":false,"model_installed":installed,"integrity_error":null,"model_path":crate::semantic_model::directory(root),"indexed_rows":coverage.embedded,"total_rows":coverage.total,"backlog":coverage.total.saturating_sub(coverage.embedded),"state":if !installed {"unavailable"} else if coverage.building() {"building"} else {"ready"},"progress":progress});
    let (bytes, files) = disk_usage(&root.0)?;
    out["disk_bytes"] = json!(bytes);
    out["disk_files"] = json!(files);
    Ok(out)
}
/// Verifies the model [`status`] found installed, hashing only files whose verified
/// stamps changed, and records the outcome in it. `doctor --no-semantic` skips this.
pub fn verify_model(root: &DataRoot, status: &mut Value) {
    if status["semantic"]["model_installed"] != true {
        return;
    }
    let error = crate::semantic_model::verify_installed(root)
        .err()
        .map(|e| format!("{e:#}"));
    status["semantic"]["model_present"] = json!(error.is_none());
    status["semantic"]["integrity_error"] = json!(error);
}
pub fn integrations(config: &crate::data::Config) -> Result<Vec<Value>> {
    Ok(config.enrollment.iter().map(|item| {
        match crate::integrations::enrolled_present(item) {
            Ok(present) => json!({"path":item.path,"kind":item.kind,"present":present,"drifted":!present,"next_action":if present{None}else{Some("agrep doctor --fix")}}),
            Err(e) => json!({"path":item.path,"state":"unreadable","drifted":true,"error":e.to_string(),"next_action":"agrep doctor --fix"}),
        }
    }).collect())
}

pub fn source_path(key: &str, agent: &str) -> String {
    if key.starts_with("token:") {
        if let Some(encoded) = key.splitn(3, ':').nth(2).and_then(|s| s.split('\0').next()) {
            let decoded = encoded
                .as_bytes()
                .chunks_exact(2)
                .map(|p| {
                    std::str::from_utf8(p)
                        .ok()
                        .and_then(|s| u8::from_str_radix(s, 16).ok())
                })
                .collect::<Option<Vec<_>>>();
            if let Some(bytes) = decoded {
                if let Ok(path) = String::from_utf8(bytes) {
                    return path;
                }
            }
        }
    }
    if key.starts_with('@') || key.starts_with("token:") {
        if let Some(adapter) = crate::ingest::registry::ADAPTERS
            .iter()
            .find(|a| a.name() == agent)
        {
            return adapter.runtime_issue_root().to_string_lossy().into_owned();
        }
    }
    key.into()
}

#[cfg(test)]
mod tests {
    // `status` failed with ENOENT when a merge worker's last SQLite connection deleted
    // index.db-wal between the walk listing the data root and examining the entry.
    #[test]
    fn an_entry_removed_after_the_listing_counts_as_absent() {
        let t = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("index.db"), b"12345").unwrap();
        std::fs::write(t.path().join("index.db-wal"), b"123").unwrap();
        let listed: Vec<_> = std::fs::read_dir(t.path())
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect();
        std::fs::remove_file(t.path().join("index.db-wal")).unwrap();
        let usage = listed
            .iter()
            .map(|p| super::disk_usage(p).unwrap())
            .fold((0, 0), |a, b| (a.0 + b.0, a.1 + b.1));
        assert_eq!(usage, (5, 1));
        assert_eq!(super::disk_usage(t.path()).unwrap(), (5, 1));
    }
}
