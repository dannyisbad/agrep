//! Read-only facts for diagnostic commands. No query parses source transcripts.
use crate::{data::DataRoot, store};
use anyhow::Result;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{fs, path::Path};
pub fn disk_bytes(path: &Path) -> Result<u64> {
    let m = fs::symlink_metadata(path)?;
    if m.file_type().is_symlink() {
        return Ok(0);
    }
    if m.is_file() {
        return Ok(m.len());
    }
    let mut n = 0;
    for e in fs::read_dir(path)? {
        n += disk_bytes(&e?.path())?;
    }
    Ok(n)
}
pub fn table_exists(c: &Connection, name: &str) -> Result<bool> {
    Ok(c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
        [name],
        |r| r.get(0),
    )?)
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
        Ok(json!({"path":source_path(&path,&agent),"derived":path.starts_with('@')||path.starts_with("token:"),"agent":agent,"bytes":r.get::<_,i64>(2)?,"mtime_ns":r.get::<_,i64>(3)?,"last_error":r.get::<_,Option<String>>(4)?,"change_token":r.get::<_,String>(5)?}))
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
    let mut out = json!({"version":env!("CARGO_PKG_VERSION"),"data_root":root.0,"format":format,"disk_bytes":if root.0.exists(){Some(disk_bytes(&root.0)?)}else{None},"index":{"state":"missing"},"sources":[],"agents":[],"semantic":{"model_present":false,"indexed_rows":0,"total_rows":0,"backlog":0},"integrations":[],"archive":{"present":root.0.join("archive").is_dir()}});
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
    out["index"] = json!({"state":"ready","counts":counts,"last_refresh":state(&c,"refreshed")?});
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
    let model = crate::semantic_model::directory(root);
    let present = crate::semantic::installed(root);
    let integrity = if present {
        crate::semantic_model::verify_installed(root)
            .err()
            .map(|e| format!("{e:#}"))
    } else {
        None
    };
    out["semantic"] = json!({"model_present":present&&integrity.is_none(),"model_installed":present,"integrity_error":integrity,"model_path":model,"indexed_rows":coverage.embedded,"total_rows":coverage.total,"backlog":coverage.total.saturating_sub(coverage.embedded)});
    out["disk_bytes"] = json!(disk_bytes(&root.0)?);
    Ok(out)
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
